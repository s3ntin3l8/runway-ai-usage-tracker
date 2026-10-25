"""Parse Hermes Agent SQLite databases into UsageEventPush records.

Hermes Agent stores sessions and usage in SQLite databases (WAL mode).
Default profile lives in ``~/.hermes/state.db``, and named profiles live in
``~/.hermes/profiles/<profile_name>/state.db`` (e.g. ``profiles/review-bot/state.db``).

Usage accounting is recorded in two tables:
  - ``sessions``: session metadata (id, cwd, git_branch, profile_name, started_at, etc.)
  - ``session_model_usage``: multi-model / task breakdown:
      - (session_id, model, billing_provider, billing_base_url, billing_mode, task) PK
      - api_call_count, input_tokens, output_tokens, cache_read_tokens,
        cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
        first_seen, last_seen

Because ``session_model_usage`` accumulates tokens over the lifetime of a task
slice, this extractor tracks incremental deltas using a local watermark state
file so ongoing sessions emit only fresh tokens per cycle without duplicate
drops or double-counting.
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
from urllib.parse import urlparse

# Allow importing from app/ when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.source_apps import HERMES  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from app.services.model_normalization import normalize_gemini_model  # noqa: E402
from scripts.sidecar_pkg.canonical_providers import (  # noqa: E402
    SHARED_CANONICAL_PROVIDER_MAP,
    CanonicalProviderTuple,
)
from scripts.sidecar_pkg.sqlite_cursor import CursorReadError, iter_cursor  # noqa: E402

logger = logging.getLogger("runway.sidecar.hermes")

# Upstream billing_provider -> (canonical provider_id, explicit account override or None).
_HERMES_CANONICAL_MAP: dict[str, CanonicalProviderTuple] = {
    **SHARED_CANONICAL_PROVIDER_MAP,
    "kimi-coding": ("kimi_coding", None),
    "kimi": ("kimi_coding", None),
    "minimax": ("minimax", None),
    "minimax-oauth": ("minimax", None),
    "opencode-go": ("opencode", None),
    "opencode-zen": ("opencode", None),
    "opencode": ("opencode", None),
    "opencode-free": ("opencode-free", None),
    "deepseek-api": ("deepseek", None),
    "anthropic": ("anthropic", None),
    "gemini": ("gemini", None),
    "ollama": ("ollama", None),
    "xai-oauth": ("xai", None),
    "xai-api": ("xai", None),
}


def _is_free_model(model: str) -> bool:
    """Return True if model name indicates a free-tier model.

    Deliberately narrower than `app.services.model_normalization.is_free_tier_model_id`,
    which also matches the "openrouter/free/*" prefix form. This one decides
    which provider bucket a Hermes session files under; the shared one decides
    whether an id may be billed at a paid rate. Widening this would silently
    re-route "openrouter/free/*" sessions from "opencode" to "opencode-free".
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if m.endswith((":free", "-free", "/free")):
        return True
    last_token = m.replace(":", "/").split("/")[-1]
    return last_token == "free" or last_token.endswith("-free")


def _url_host_matches(url: str, *domains: str) -> bool:
    """Safely check if a URL's hostname matches or is a subdomain of any candidate domain."""
    if not url:
        return False
    try:
        raw = url.strip()
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        for domain in domains:
            d = domain.strip().lower()
            if host == d or host.endswith(f".{d}"):
                return True
        return False
    except Exception:
        return False


def map_hermes_canonical(billing_provider: str) -> tuple[str, str | None] | None:
    """Return the canonical provider and optional explicit account override."""
    return _HERMES_CANONICAL_MAP.get((billing_provider or "").strip().lower())


def map_hermes_provider_id(billing_provider: str) -> str:
    """Map a Hermes billing_provider to a Runway provider_id."""
    bp = (billing_provider or "").strip().lower()
    if not bp:
        return "hermes"
    canonical = map_hermes_canonical(bp)
    if canonical:
        return canonical[0]
    return f"hermes-{bp}"


def resolve_hermes_provider_and_canonical(
    billing_provider: str,
    billing_base_url: str,
    model: str,
    session_billing_provider: str = "",
    session_billing_base_url: str = "",
    session_model: str = "",
    session_billing_mode: str = "",
) -> tuple[str, CanonicalProviderTuple | None]:
    """Resolve raw and session metadata to (target_provider_id, canonical_tuple_or_None)."""
    bp = (billing_provider or "").strip().lower()
    sbp = (session_billing_provider or "").strip().lower()
    base_url = (billing_base_url or session_billing_base_url or "").strip().lower()
    m = (model or session_model or "").strip()
    m_lower = m.lower()

    # 1. Model-specific heuristics when billing_provider is missing, hermes, or generic
    if not bp or bp in ("hermes", "auto", "default", "custom"):
        if m_lower.startswith("kimi-") or m_lower == "kimi-for-coding":
            bp = "kimi-coding"
        elif m_lower.startswith("grok-"):
            bp = "xai-oauth"
        elif m_lower.startswith("minimax-") or m_lower == "minimax":
            bp = "minimax-oauth"
        elif sbp and sbp not in ("hermes", "auto", "default", "custom"):
            bp = sbp
        elif _url_host_matches(base_url, "opencode.ai") or sbp.startswith("opencode"):
            bp = "opencode"
        elif _url_host_matches(base_url, "api.x.ai", "x.ai"):
            bp = "xai-oauth"
        elif _url_host_matches(
            base_url, "api.kimi.com", "kimi.com", "api.moonshot.cn", "moonshot.cn"
        ):
            bp = "kimi-coding"
        elif _url_host_matches(base_url, "api.minimax.io", "minimax.io"):
            bp = "minimax-oauth"
        elif _url_host_matches(base_url, "openrouter.ai"):
            bp = "openrouter"

    # 2. OpenCode tier splitting: free models vs paid/subscription
    is_opencode = (
        bp in ("opencode", "opencode-go", "opencode-zen", "auto")
        or _url_host_matches(base_url, "opencode.ai")
        or sbp.startswith("opencode")
    )
    if is_opencode:
        mode = (session_billing_mode or "").strip().lower()
        if mode == "free" or _is_free_model(m):
            return "opencode-free", ("opencode-free", None)
        return "opencode", ("opencode", None)

    # 3. Canonical map lookup
    canonical = map_hermes_canonical(bp)
    if canonical is not None:
        return canonical[0], canonical

    # 4. Fallback provider ID
    provider_id = map_hermes_provider_id(bp)
    return provider_id, None


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
WatermarkChanges = dict[str, dict[str, dict[Any | None]]]

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
    for path in live_paths:
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


def parse_hermes_events(
    db_paths: list[Path],
    account_id: str,
    since: datetime,
    canonical_hints: dict[str, dict[str, str]] | None = None,
    state_file: Path | None = None,
) -> list[UsageEventPush]:
    """Extract UsageEventPush records from Hermes SQLite database files.

    Args:
        db_paths: List of paths to Hermes state.db SQLite files.
        account_id: Fallback account ID (e.g. 'default' or operator email).
        since: Only consider model usage records strictly after this timestamp.
        canonical_hints: Optional {canonical_provider_id: {origin: account_id}} map.
        state_file: Optional path to JSON file tracking slice token watermarks.
    """
    # Note on watermark scoping & bounding: hermes_watermark.jsonl stores token
    # and call high-water marks scoped per account_id and (resolved_db_path,
    # profile, session, model, provider, base_url, mode, task) slice. The
    # database path is stored once per database, and the file is an append-only
    # log: a cycle appends the slices that changed instead of rewriting the
    # whole state, so the bytes written per day no longer grow with the file.
    # Each slice records 'last_seen', and a slice is forgotten once it has been
    # idle for WATERMARK_PRUNE_IDLE_DAYS and its source is provably dead:
    # either its database file is gone, or its row is gone from a database that
    # is still there (see _prune_stale_watermark_slices).
    state_path = state_file or _default_watermark_state_path()
    watermark_state, log_lines = _load_hermes_watermark(state_path)
    if not state_path.exists():
        # The legacy flat file: read it once and write it in the new format.
        # It is left on disk afterwards, so a reverted sidecar still finds it.
        legacy_path = _legacy_watermark_state_path(state_path)
        if legacy_path.exists():
            watermark_state = _migrate_legacy_watermark(
                legacy_path,
                state_path,
                account_id=account_id,
                live_paths=[str(path.resolve()) for path in db_paths],
            )
            log_lines = len(watermark_state)

    # The slices whose row is still in the database, per database. A slice
    # whose row is gone can never advance again, so its mark is dead weight
    # once the session has been quiet for long enough (#958).
    live_slices: dict[str, set[str]] = {}

    # The marks that changed, staged per database and committed only when a
    # database was read to the end: a failed cursor must not commit deltas
    # for a partial scan.
    state_updates: dict[str, dict[str, dict[str, Any]]] = {}

    since_epoch = since.timestamp()
    events: list[UsageEventPush] = []

    for db_path in db_paths:
        if not db_path.exists():
            continue

        # Infer profile name from directory structure
        if db_path.parent.parent.name == "profiles":
            profile_name = db_path.parent.name
        else:
            profile_name = "default"

        uri_path = f"file:{db_path.resolve()}?mode=ro"
        try:
            conn = sqlite3.connect(uri_path, uri=True)
        except Exception as exc:
            logger.warning("Failed to query Hermes state DB at %s: %s", db_path, exc)
            continue

        event_start = len(events)
        db_updates: dict[str, dict[str, Any]] = {}
        db_key = str(db_path.resolve())
        try:
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.cursor()
                # Check whether session_model_usage table exists
                tbl_check = cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_model_usage'"
                ).fetchone()
                if not tbl_check:
                    continue

                query = """
                    SELECT
                        smu.session_id,
                        smu.model,
                        smu.billing_provider,
                        smu.billing_base_url,
                        smu.billing_mode,
                        smu.task,
                        smu.api_call_count,
                        smu.input_tokens,
                        smu.output_tokens,
                        smu.cache_read_tokens,
                        smu.cache_write_tokens,
                        smu.reasoning_tokens,
                        smu.estimated_cost_usd,
                        smu.actual_cost_usd,
                        smu.first_seen,
                        smu.last_seen,
                        s.cwd,
                        s.git_branch,
                        s.source,
                        COALESCE(s.profile_name, ?) as profile_name,
                        s.billing_provider as session_billing_provider,
                        s.billing_base_url as session_billing_base_url,
                        s.billing_mode as session_billing_mode,
                        s.model as session_model
                    FROM session_model_usage smu
                    LEFT JOIN sessions s ON smu.session_id = s.id
                    WHERE MAX(COALESCE(smu.last_seen, 0), COALESCE(smu.first_seen, 0)) > ?
                       OR (smu.last_seen IS NULL AND smu.first_seen IS NULL)
                    ORDER BY MAX(COALESCE(smu.last_seen, 0), COALESCE(smu.first_seen, 0)) ASC
                """
                rows = iter_cursor(cur.execute(query, (profile_name, since_epoch)))
            except Exception as exc:
                logger.warning("Failed to query Hermes state DB at %s: %s", db_path, exc)
                continue

            try:
                for row in rows:
                    session_id = row["session_id"]
                    model = row["model"] or "unknown"
                    billing_provider = row["billing_provider"] or ""
                    billing_base_url = row["billing_base_url"] or ""
                    billing_mode = row["billing_mode"] or ""
                    task = row["task"] or ""

                    raw_last_seen = row["last_seen"]
                    raw_first_seen = row["first_seen"]
                    if raw_last_seen is None and raw_first_seen is None:
                        logger.warning(
                            "Hermes session_model_usage row for session %s has both last_seen and first_seen NULL; skipping",
                            session_id,
                        )
                        continue

                    last_seen = float(
                        max(
                            raw_last_seen if raw_last_seen is not None else 0.0,
                            raw_first_seen if raw_first_seen is not None else 0.0,
                        )
                    )
                    # Belt-and-suspenders guard against floating-point precision / rounding
                    # differences between SQLite and Python timestamps; SQL query already filters smu.last_seen > since_epoch.
                    if last_seen <= since_epoch:
                        continue

                    try:
                        ts = datetime.fromtimestamp(last_seen, tz=UTC)
                    except Exception:
                        continue

                    curr_calls = int(row["api_call_count"] or 0)
                    curr_in = int(row["input_tokens"] or 0)
                    curr_out = int(row["output_tokens"] or 0)
                    curr_cache_read = int(row["cache_read_tokens"] or 0)
                    curr_cache_write = int(row["cache_write_tokens"] or 0)
                    curr_reasoning = int(row["reasoning_tokens"] or 0)

                    actual_cost = row["actual_cost_usd"]
                    est_cost = row["estimated_cost_usd"]
                    # Provider-supplied actual_cost_usd is authoritative; estimated fallback only when NULL
                    curr_cost = float(actual_cost if actual_cost is not None else (est_cost or 0.0))

                    # The watermark key identifies this slice within the DB per
                    # account (including billing_base_url and billing_mode).
                    # The database path is stored once per database, the
                    # account once per account, and the slice identity is what
                    # is left of the old flat key.
                    slice_key = _hermes_slice_suffix(
                        profile_name,
                        session_id,
                        model,
                        billing_provider,
                        billing_base_url,
                        billing_mode,
                        task,
                    )
                    staged = db_updates.get(account_id, {}).get(slice_key)
                    prev = (
                        staged
                        if staged is not None
                        else watermark_state.get(db_key, {}).get(account_id, {}).get(slice_key, {})
                    )

                    prev_in = int(prev.get("input_tokens", 0))
                    prev_out = int(prev.get("output_tokens", 0))
                    prev_cache_read = int(prev.get("cache_read_tokens", 0))
                    prev_cache_write = int(prev.get("cache_write_tokens", 0))
                    prev_reasoning = int(prev.get("reasoning_tokens", 0))
                    prev_cost = float(prev.get("cost_usd", 0.0))
                    prev_calls = int(prev.get("api_call_count", 0))

                    delta_in = max(0, curr_in - prev_in)
                    delta_out = max(0, curr_out - prev_out)
                    delta_cache_read = max(0, curr_cache_read - prev_cache_read)
                    delta_cache_write = max(0, curr_cache_write - prev_cache_write)
                    delta_reasoning = max(0, curr_reasoning - prev_reasoning)
                    delta_cost = max(0.0, curr_cost - prev_cost)

                    has_tokens = any(
                        (delta_in, delta_out, delta_cache_read, delta_cache_write, delta_reasoning)
                    )
                    has_cost = delta_cost > 0
                    is_new_calls = curr_calls > 0 and curr_calls > prev_calls

                    if not (has_tokens or has_cost or is_new_calls):
                        continue

                    # Determine provider_id and canonical mapping
                    target_provider_id, canonical = resolve_hermes_provider_and_canonical(
                        billing_provider=billing_provider,
                        billing_base_url=billing_base_url,
                        model=model,
                        session_billing_provider=row["session_billing_provider"] or "",
                        session_billing_base_url=row["session_billing_base_url"] or "",
                        session_model=row["session_model"] or "",
                        session_billing_mode=row["session_billing_mode"] or "",
                    )
                    if canonical is not None:
                        canonical_provider_id, account_override = canonical
                        target_provider_id = canonical_provider_id

                        if account_override is not None:
                            event_account_id = account_override
                            event_account_source = "tag"
                        elif canonical_hints:
                            provider_hints = canonical_hints.get(canonical_provider_id, {})
                            canonical_hint = provider_hints.get(f"provider:{canonical_provider_id}")
                            if not canonical_hint and canonical_provider_id == "opencode-free":
                                canonical_hint = canonical_hints.get("opencode", {}).get(
                                    "provider:opencode"
                                )
                            if canonical_hint:
                                event_account_id = canonical_hint
                                event_account_source = "tag"
                            else:
                                # Hermes host identity doesn't prove which account was used
                                # through an underlying canonical provider. Hold back as pending.
                                event_account_id = "default"
                                event_account_source = "default"
                        else:
                            event_account_id = "default"
                            event_account_source = "default"
                    else:
                        event_account_id = account_id
                        event_account_source = None

                    # Monotonic event ID incorporating slice call count and emission sequence
                    emission_seq = int(prev.get("emission_seq", 0)) + 1
                    task_slug = task if task else "main"
                    event_id = f"hermes|{profile_name}|{session_id}|{model}|{task_slug}|c{curr_calls}s{emission_seq}"

                    cost_usd: float | None = delta_cost if delta_cost > 0.0 else None

                    events.append(
                        UsageEventPush(
                            provider_id=target_provider_id,
                            source_app=HERMES,
                            account_id=event_account_id,
                            account_source=event_account_source,
                            event_id=event_id,
                            ts=ts.isoformat(),
                            # Only the Gemini pricing rows are keyed on the versioned bucket
                            # (flash-2.5, ...), so a raw id would miss the lookup; other
                            # providers (OpenCode, Antigravity) price on the raw model id.
                            # The raw id stays in event_id so the idempotency key is stable.
                            model_id=(
                                normalize_gemini_model(model)
                                if target_provider_id == "gemini"
                                else model
                            ),
                            session_id=session_id,
                            subagent_type=task or None,
                            cwd=row["cwd"] or None,
                            git_branch=row["git_branch"] or None,
                            tokens_input=delta_in,
                            tokens_output=delta_out,
                            tokens_cache_read=delta_cache_read,
                            tokens_cache_create=delta_cache_write,
                            tokens_reasoning=delta_reasoning,
                            cost_usd=cost_usd,
                            entrypoint=(
                                f"hermes-{row['source']}"
                                if row["source"] and row["source"] != "api_server"
                                else "hermes"
                            ),
                            kind="message",
                        )
                    )

                    # Update tracked watermark monotonically
                    db_updates.setdefault(account_id, {})[slice_key] = {
                        "input_tokens": max(curr_in, prev_in),
                        "output_tokens": max(curr_out, prev_out),
                        "cache_read_tokens": max(curr_cache_read, prev_cache_read),
                        "cache_write_tokens": max(curr_cache_write, prev_cache_write),
                        "reasoning_tokens": max(curr_reasoning, prev_reasoning),
                        "cost_usd": max(curr_cost, prev_cost),
                        "api_call_count": max(curr_calls, prev_calls),
                        "emission_seq": emission_seq,
                        "last_seen": max(last_seen, float(prev.get("last_seen", 0.0))),
                    }
            except CursorReadError as exc:
                del events[event_start:]
                logger.warning("Failed to query Hermes state DB at %s: %s", db_path, exc)
                continue

            # The rows that are still there are the slices that can still
            # advance. A database whose rows cannot be listed is left out, so
            # that its slices keep their marks.
            live = _live_slice_suffixes(conn, profile_name)
            if live is not None:
                live_slices[db_key] = live

            # A failed cursor must not commit deltas for a partial DB scan.
            for account, slices in db_updates.items():
                watermark_state.setdefault(db_key, {}).setdefault(account, {}).update(slices)
                state_updates.setdefault(db_key, {}).setdefault(account, {}).update(slices)
        finally:
            conn.close()

    # Forget the slices that can no longer move. This runs on every cycle,
    # not only when something changed: a cycle that emits nothing can still
    # shrink the file, and the shrink has to be written out or the next cycle
    # would put the pruned slices back.
    pruned = _prune_stale_watermark_slices(
        watermark_state,
        idle_cutoff=datetime.now(UTC).timestamp() - WATERMARK_PRUNE_IDLE_DAYS * 86400,
        live_slices=live_slices,
    )
    if pruned:
        logger.info("Pruned %d stale Hermes watermark slice(s) from %s", len(pruned), state_path)

    # The slices that changed are appended to the log. A forgotten slice is
    # appended as a null, so the log says it is gone.
    changes: WatermarkChanges = {}
    for db_key, accounts in state_updates.items():
        for account, slices in accounts.items():
            changes.setdefault(db_key, {}).setdefault(account, {}).update(slices)
    for db_key, account, slice_key in pruned:
        changes.setdefault(db_key, {}).setdefault(account, {})[slice_key] = None

    if changes:
        appended = _append_hermes_watermark(state_path, changes)
        # The log grows by one line per changed database per cycle. Once it
        # holds more lines than the state it carries, the state is the
        # cheaper file to write, so the log is folded back into it.
        live_slices_count = sum(
            len(slices) for accounts in watermark_state.values() for slices in accounts.values()
        )
        if log_lines + appended > max(WATERMARK_COMPACT_MIN_LINES, live_slices_count):
            _compact_hermes_watermark(state_path, watermark_state)

    return events
