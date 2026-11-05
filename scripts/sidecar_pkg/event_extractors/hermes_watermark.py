"""Hermes watermark log: discovery, legacy migration, compaction, and pruning.

Split out of ``hermes.py``. The parser stays in ``hermes`` and calls these
names through that module, so a test patch of ``hermes._append_hermes_watermark``,
``hermes._live_slice_suffixes`` or ``hermes.WATERMARK_COMPACT_MIN_LINES`` still
intercepts ``parse_hermes_events``.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.sqlite_cursor import CursorReadError, iter_cursor

logger = logging.getLogger("runway.sidecar.hermes")


def _discover_hermes_db_paths() -> list[Path]:
    """Discover all Hermes state.db files: default profile and named profiles.

    Canonicalizes paths with resolve() so symlinks or overlapping roots (e.g.
    HERMES_HOME pointing to ~/.hermes) are deduplicated at discovery.
    """
    paths: list[Path] = []
    seen_canonical: set[Path] = set()

    def _add_if_valid(p: Path) -> None:
        if p.exists():
            resolved = p.resolve()
            if resolved not in seen_canonical:
                seen_canonical.add(resolved)
                paths.append(p)

    # 1. HERMES_HOME env override (default DB and named profiles)
    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        home_path = Path(hermes_home).expanduser()
        _add_if_valid(home_path / "state.db")
        custom_profiles_dir = home_path / "profiles"
        if custom_profiles_dir.is_dir():
            for prof_db in sorted(custom_profiles_dir.glob("*/state.db")):
                _add_if_valid(prof_db)

    # 2. Standard default profile
    default_dir = Path("~/.hermes").expanduser()
    _add_if_valid(default_dir / "state.db")

    # 3. Named profiles in ~/.hermes/profiles/*/state.db
    profiles_dir = default_dir / "profiles"
    if profiles_dir.is_dir():
        for prof_db in sorted(profiles_dir.glob("*/state.db")):
            _add_if_valid(prof_db)

    return paths


def _watermark_state_dir() -> Path:
    """Return the directory that holds the Hermes watermark files.

    Mirrors ``sidecar.get_sidecar_dir()`` (``RUNWAY_CONFIG_DIR``, then
    ``%APPDATA%\\runway\\sidecar`` on Windows, then
    ``~/.config/runway/sidecar``) without importing it: the frozen app runs
    ``scripts/sidecar.py`` as a script, so a module import would create a
    second copy of its state. A test pins the two together. Resolution goes
    through ``Path.home()`` rather than a literal ``~`` so it follows the
    same home the rest of the sidecar uses.

    Upgrade note (Windows): earlier builds read ``~/.config/runway/sidecar``
    here even on Windows, so an existing install's watermark file there is
    orphaned and the first run re-scans the whole Hermes history. The server
    deduplicates on ``event_id``, so no usage is counted twice.
    """
    config_dir = os.getenv("RUNWAY_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "sidecar"
    if platform.system() == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data:
            return Path(app_data) / "runway" / "sidecar"
        return Path.home() / "AppData" / "Roaming" / "runway" / "sidecar"
    return Path.home() / ".config" / "runway" / "sidecar"


def _default_watermark_state_path() -> Path:
    """Return the Hermes watermark log path."""
    return _watermark_state_dir() / "hermes_watermark.jsonl"


def _legacy_watermark_state_path(state_path: Path) -> Path:
    """Return the path of the legacy (flat JSON) watermark file.

    The legacy file is read once, when the log does not exist yet, and is
    then left alone. It is not deleted: a sidecar that is reverted reads it
    and re-reports only the usage since the migration, where a missing file
    would make it re-report the whole Hermes history.
    """
    return state_path.with_name("hermes_watermark.json")


def _legacy_migration_marker_path(state_path: Path) -> Path:
    """Return the path of the marker that records the one-time legacy read.

    The legacy file stays on disk, so the marker is what keeps it from being
    read a second time: without it, a deleted watermark log would be
    refilled with the legacy baseline and the documented re-scan would
    silently do nothing on every upgraded install.
    """
    legacy_path = _legacy_watermark_state_path(state_path)
    return legacy_path.with_name(legacy_path.name + ".migrated")


# The watermark state, nested: database path -> account -> slice -> mark.
#
# The database path is stored once per database instead of once per slice,
# and the file is an append-only log: a cycle appends the slices that
# changed rather than rewriting the whole state. The legacy format was a
# flat JSON object keyed by "{account}|{db_path}|{profile}|{session}|..." --
# see _split_legacy_watermark_key for how those keys are read.
WatermarkState = dict[str, dict[str, dict[str, Any]]]

# The slices that changed in a cycle: database path -> account -> slice ->
# the new mark of the slice, or None when the slice is forgotten.
WatermarkChanges = dict[str, dict[str, dict[str, Any | None]]]

# The log is compacted once it holds more lines than the state it carries.
# Below that the log is the cheaper file to write; above it, the log costs
# more than the state it duplicates.
WATERMARK_COMPACT_MIN_LINES = 64


def _load_hermes_watermark(path: Path) -> tuple[WatermarkState, int]:
    """Read the watermark log.

    Returns the state and the number of lines the log holds, which is what
    the compaction looks at.
    """
    state: WatermarkState = {}
    lines = 0
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return state, 0
    except OSError as exc:
        logger.warning("Failed to read the Hermes watermark log at %s: %s", path, exc)
        return state, 0
    for number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        lines += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A torn last line (a crash during the write) or a damaged one.
            # The log is a cache: skipping a line beats refusing to read the
            # file, which would lose every mark in it.
            logger.warning(
                "Hermes watermark log line %d of %s is not valid JSON, ignoring it", number, path
            )
            continue
        _merge_watermark_entry(state, entry)
    return state, lines


def _merge_watermark_entry(state: WatermarkState, entry: Any) -> None:
    """Merge one log entry into the state."""
    if not isinstance(entry, dict):
        return
    db_path = entry.get("db")
    accounts = entry.get("accounts")
    if not isinstance(db_path, str) or not isinstance(accounts, dict):
        return
    for account, slices in accounts.items():
        if not isinstance(slices, dict):
            continue
        for slice_key, mark in slices.items():
            if mark is None:
                # A forgotten slice. The account is only touched when it is
                # there: an entry that is left empty would keep the database
                # in the state for nothing.
                account_target = state.get(db_path, {}).get(account)
                if account_target is not None:
                    account_target.pop(slice_key, None)
            elif isinstance(mark, dict):
                state.setdefault(db_path, {}).setdefault(account, {})[slice_key] = mark


def _append_hermes_watermark(path: Path, changes: WatermarkChanges) -> int:
    """Append the changed slices to the watermark log.

    The write is flushed and synced before it returns: the emission counter
    of a slice lives in the watermark *and* in the event id, so an update
    that is lost means an event id the server has already seen -- and the
    usage behind it would be dropped.

    Returns the number of lines written.
    """
    lines = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for db_path, accounts in changes.items():
                handle.write(
                    json.dumps({"db": db_path, "accounts": accounts}, separators=(",", ":")) + "\n"
                )
                lines += 1
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        logger.warning("Failed to append to the Hermes watermark log at %s: %s", path, exc)
        return 0
    return lines


def _split_legacy_watermark_key(
    flat_key: str, account_id: str, live_paths: list[str]
) -> tuple[str, str, str] | None:
    """Split a legacy watermark key into (database path, account, slice).

    The legacy key is ``{account}|{db_path}|{slice}``. The account is an
    opaque string that may hold a ``|``, so the path is read out by the
    rules below rather than by field position. They are ordered by how
    exact they are:

    1. The path is one of the discovered databases. The key holds it whole,
       so its boundaries are known exactly -- even when the path itself
       holds a ``|``.
    2. The key starts with the account this collector runs under, so the
       path is the field behind it.
    3. The path is the first field behind a separator that looks like an
       absolute path.

    Returns None when the key holds no path at all: such a key is not the
    mark of any slice, so there is nothing to keep.
    """
    # The longest path first. A path can be a prefix of another one --
    # "/data/a" of "/data/a|b|c" -- and the character behind it in the
    # longer path is then a "|", so the marker "|/data/a|" also matches
    # inside "|/data/a|b|c|". On discovery order the shorter path would
    # win: the mark of "/data/a|b|c" would be filed under "/data/a", a
    # database that does not exist, and would be reclaimed as a dead slice
    # -- the real baseline lost, and the session's whole lifetime reported
    # a second time.
    for path in sorted(live_paths, key=len, reverse=True):
        marker = f"|{path}|"
        index = flat_key.rfind(marker)
        if index != -1:
            return path, flat_key[:index], flat_key[index + len(marker) :]

    prefix = f"{account_id}|"
    if flat_key.startswith(prefix):
        db_path, separator, suffix = flat_key[len(prefix) :].partition("|")
        if separator and suffix and _looks_like_absolute_path(db_path):
            return db_path, account_id, suffix

    index = 0
    while True:
        index = flat_key.find("|", index)
        if index == -1:
            return None
        db_path, separator, suffix = flat_key[index + 1 :].partition("|")
        if separator and suffix and _looks_like_absolute_path(db_path):
            return db_path, flat_key[:index], suffix
        index += 1


def _write_legacy_migration_marker(marker_path: Path, legacy_path: Path, marks: int) -> None:
    """Record that the legacy file was read, so it is never read again.

    The marker is what makes the re-scan work: deleting the watermark log
    has to leave an empty state, not the legacy baseline. It is written
    only after the log itself is on disk, so a failed write leaves the
    migration to be retried rather than dropping the baseline for good.
    """
    payload = {
        "migrated_at": datetime.now(UTC).isoformat(),
        "legacy_file": legacy_path.name,
        "marks": marks,
    }
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning(
            "Failed to write the Hermes watermark migration marker at %s: %s", marker_path, exc
        )


def _migrate_legacy_watermark(
    legacy_path: Path,
    state_path: Path,
    *,
    account_id: str,
    live_paths: list[str],
) -> WatermarkState:
    """Read the legacy flat watermark and write it in the new format.

    Every mark that can be read is carried over: the mark is the difference
    between what the sidecar already reported and what it would report
    next, so a mark that is lost is a session whose whole lifetime is
    reported a second time.

    A key that holds no database path is not the mark of any slice and is
    dropped -- with a warning, because the legacy file keeps it.
    """
    state: WatermarkState = {}
    unreadable = 0
    try:
        content = legacy_path.read_text(encoding="utf-8").strip()
        legacy = json.loads(content) if content else {}
    except Exception as exc:
        logger.warning("Failed to read the legacy Hermes watermark at %s: %s", legacy_path, exc)
        return state
    if not isinstance(legacy, dict):
        logger.warning("Legacy Hermes watermark at %s did not contain a JSON object", legacy_path)
        return state

    for flat_key, mark in legacy.items():
        if not isinstance(mark, dict):
            continue
        split = _split_legacy_watermark_key(flat_key, account_id, live_paths)
        if split is None:
            unreadable += 1
            continue
        db_path, account, slice_key = split
        state.setdefault(db_path, {}).setdefault(account, {})[slice_key] = mark
    if unreadable:
        logger.warning(
            "Legacy Hermes watermark at %s held %d key(s) without a database path, ignoring them",
            legacy_path,
            unreadable,
        )

    _compact_hermes_watermark(state_path, state)
    if state_path.exists():
        # The log is on disk, so the legacy file has been read for good.
        # The marker keeps a deleted log from being refilled with the
        # legacy baseline, which would make the documented re-scan a
        # no-op on every upgraded install.
        _write_legacy_migration_marker(
            _legacy_migration_marker_path(state_path),
            legacy_path,
            sum(len(slices) for accounts in state.values() for slices in accounts.values()),
        )
    return state


def _compact_hermes_watermark(path: Path, state: WatermarkState) -> None:
    """Rewrite the watermark log with one line per database.

    The log grows by one line per changed database per cycle. The
    compaction folds those into one line per database, which is also the
    shape that stores the database path once. Every mark is written out,
    so a compaction cannot lose a baseline.
    """
    lines: list[str] = []
    for db_path, accounts in state.items():
        payload = {account: slices for account, slices in accounts.items() if slices}
        if not payload:
            continue
        lines.append(json.dumps({"db": db_path, "accounts": payload}, separators=(",", ":")))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
            if lines:
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("Failed to compact the Hermes watermark log at %s: %s", path, exc)


# A slice's high-water mark is forgotten only after it has been idle this
# long AND its database file is gone. Both conditions matter:
#   - "the file is gone" alone would drop the mark of a live database that a
#     backup tool renamed aside for a second, and Hermes would then report
#     that session's whole lifetime as new usage.
#   - "idle for long enough" alone would drop the mark of a live session that
#     resumes later, with the same result.
# 30 days is long enough that an active session keeps its mark, and short
# enough that the watermark does not keep a deleted database's slices forever.
WATERMARK_PRUNE_IDLE_DAYS = 30


def _looks_like_absolute_path(text: str) -> bool:
    """True when the text could be a database path rather than a key fragment."""
    if text.startswith("/"):
        return True
    # Windows: C:\... or \\server\share
    if len(text) >= 3 and text[1] == ":" and text[2] in "\\/":
        return True
    if text.startswith("\\\\"):
        return True
    return False


def _database_path_is_gone(db_path: str) -> bool:
    """True when the file behind a watermark key's path is really gone.

    ``Path.exists()`` answers False for a path it cannot stat (a permission
    error, a vanished mount), and "I cannot tell" is not "it is gone": the
    mark of a database we merely fail to read has to stay.
    """
    try:
        os.stat(db_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


# The slice identity is the primary key of session_model_usage, so the rows
# that are still there are exactly the slices that can still advance.
_LIVE_SLICE_QUERY = """
    SELECT session_id, model, billing_provider, billing_base_url, billing_mode, task
    FROM session_model_usage
"""


def _hermes_slice_suffix(
    profile_name: str,
    session_id: Any,
    model: Any,
    billing_provider: Any,
    billing_base_url: Any,
    billing_mode: Any,
    task: Any,
) -> str:
    """Return the slice part of a watermark key.

    A watermark key is ``{account}|{resolved_db_path}|{slice}``, and the slice
    is everything behind the path. It is built here for the key *and* for the
    lookup of the slices that still exist, so the two cannot drift apart: a
    comparison of two different strings would report a live slice as gone and
    drop its high-water mark.
    """
    return (
        f"{profile_name}|{session_id}|{model or 'unknown'}|{billing_provider or ''}"
        f"|{billing_base_url or ''}|{billing_mode or ''}|{task or ''}"
    )


def _live_slice_suffixes(conn: sqlite3.Connection, profile_name: str) -> set[str] | None:
    """Return the slices of a database whose row is still there.

    ``None`` means "could not tell", and the caller then has to keep every
    slice: a database we merely fail to read is not a database whose rows are
    gone.
    """
    suffixes: set[str] = set()
    try:
        for row in iter_cursor(conn.execute(_LIVE_SLICE_QUERY)):
            suffixes.add(
                _hermes_slice_suffix(
                    profile_name,
                    row["session_id"],
                    row["model"],
                    row["billing_provider"],
                    row["billing_base_url"],
                    row["billing_mode"],
                    row["task"],
                )
            )
    except (sqlite3.Error, CursorReadError) as exc:
        logger.warning("Failed to list the live Hermes slices: %s", exc)
        return None
    return suffixes


def _prune_stale_watermark_slices(
    watermark_state: WatermarkState,
    *,
    idle_cutoff: float,
    live_slices: dict[str, set[str]] | None = None,
) -> list[tuple[str, str, str]]:
    """Forget the high-water marks of the slices that can no longer move.

    The watermark only ever grew. A deleted profile, a test fixture that was
    unpacked and removed or a folder the user cleaned up left its slices
    behind forever. Pruning the slices that are provably dead reclaims them.

    A slice is forgotten only when **all** of these hold:

    * its ``last_seen`` is older than ``idle_cutoff`` -- the session has been
      quiet long enough that its row cannot advance unless a writer reappears,
    * and its source is provably dead, which is one of

      * **its database file no longer exists**, or
      * **its row is gone from the database** (``live_slices`` says which
        slices are still there).

    The idle window is what keeps this safe. The server deduplicates usage
    events on the exact ``(provider_id, event_id)`` and keeps the first row,
    but a re-emitted Hermes event carries a restarted counter
    (``c<calls>s<emission_seq>``) and therefore a *different* id, so the
    server cannot recognise it. With no baseline the extractor would report
    the session's whole lifetime as new usage, and the server would count it
    twice. Keeping the mark of everything that could still move -- an active
    session, a database that exists but was not discovered, a database whose
    rows could not be listed -- is what prevents that.

    The row check is the second half of that rule (#958). A session Hermes
    deleted leaves its usage row behind as a tombstone in the watermark, and
    the watermark of a long-lived install then grows with every session that
    ever ran. A row that is gone cannot advance, so its mark is dead weight --
    as long as the session has been quiet long enough that the row cannot
    come back with the counters it had.

    Args:
        watermark_state: The watermark, pruned in place.
        idle_cutoff: The timestamp below which a slice counts as idle.
        live_slices: ``{resolved_db_path: {slice, ...}}`` -- the slices whose
            row is still in the database. A database that is missing from it
            (not discovered, unreadable, or a query that failed) keeps every
            slice.

    Returns the forgotten ``(database, account, slice)`` triples.
    """
    # One stat() per distinct database path, not per slice: a host with a
    # large watermark would otherwise make thousands of syscalls every cycle.
    path_is_gone: dict[str, bool] = {}

    pruned: list[tuple[str, str, str]] = []
    for db_path, accounts in list(watermark_state.items()):
        if db_path not in path_is_gone:
            # A database that exists but was not discovered may still be in
            # use; only its own rows can say so, so it keeps its slices.
            path_is_gone[db_path] = _database_path_is_gone(db_path)
        file_gone = path_is_gone[db_path]

        # The rows that are still there are the slices that can still advance.
        # A database that is missing from the map could not be listed, and a
        # database we could not list cannot say that a row is gone.
        live = (live_slices or {}).get(db_path)

        for account, slices in list(accounts.items()):
            for slice_key, mark in list(slices.items()):
                row_gone = live is not None and slice_key not in live
                if not (file_gone or row_gone):
                    continue
                if not isinstance(mark, dict):
                    # A corrupt entry: keep it rather than guess what it was.
                    continue
                try:
                    last_seen = float(mark.get("last_seen") or 0.0)
                except (TypeError, ValueError):
                    # A hand-edited or foreign timestamp: keep the key rather
                    # than guess what it meant -- and never fail the whole
                    # collection over one unreadable entry.
                    continue
                if last_seen <= 0:
                    # No timestamp: cannot show the slice is quiet, so keep it.
                    continue
                if last_seen > idle_cutoff:
                    # Recently active: the row can still advance, so the mark
                    # stays.
                    continue
                pruned.append((db_path, account, slice_key))
                del slices[slice_key]
            if not slices:
                del accounts[account]
        if not accounts:
            del watermark_state[db_path]
    return pruned
