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

Provider resolution lives in ``hermes_providers`` and the watermark log in
``hermes_watermark``. This module keeps the parser, which is what a test
patches when it replaces a watermark helper on this module.

Because ``session_model_usage`` accumulates tokens over the lifetime of a task
slice, this extractor tracks incremental deltas using a local watermark state
file so ongoing sessions emit only fresh tokens per cycle without duplicate
drops or double-counting.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow importing from app/ when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.source_apps import HERMES  # noqa: E402
from app.models.schemas import UsageEventPush  # noqa: E402
from app.services.model_normalization import normalize_gemini_model  # noqa: E402
from scripts.sidecar_pkg.sqlite_cursor import CursorReadError, iter_cursor  # noqa: E402

logger = logging.getLogger("runway.sidecar.hermes")

# Re-exported for sidecar.py, repair scripts and tests. The parser calls the
# watermark helpers through this module, so a patch of those names here still
# wins; the names below are the ones the parser itself does not reference.
__all__ = [
    "_HERMES_CANONICAL_MAP",
    "_discover_hermes_db_paths",
    "_is_free_model",
    "_split_legacy_watermark_key",
    "_url_host_matches",
    "map_hermes_canonical",
    "map_hermes_provider_id",
    "parse_hermes_events",
    "resolve_hermes_provider_and_canonical",
]

from scripts.sidecar_pkg.hermes_providers import (
    _HERMES_CANONICAL_MAP,
    _is_free_model,
    _url_host_matches,
    map_hermes_canonical,
    map_hermes_provider_id,
    resolve_hermes_provider_and_canonical,
)
from scripts.sidecar_pkg.hermes_watermark import (
    WATERMARK_COMPACT_MIN_LINES,
    WATERMARK_PRUNE_IDLE_DAYS,
    WatermarkChanges,
    _append_hermes_watermark,
    _compact_hermes_watermark,
    _default_watermark_state_path,
    _discover_hermes_db_paths,
    _hermes_slice_suffix,
    _legacy_migration_marker_path,
    _legacy_watermark_state_path,
    _live_slice_suffixes,
    _load_hermes_watermark,
    _migrate_legacy_watermark,
    _prune_stale_watermark_slices,
    _split_legacy_watermark_key,
)


def _stat_db_path(db_path: str) -> os.stat_result:
    """Stat a watermark database path.

    ``_prune_stale_watermark_slices`` calls this rather than ``os.stat`` so a
    test that replaces ``hermes.os.stat`` still decides which slices are gone.
    """
    return os.stat(db_path)


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
        state_file: Optional path to the watermark log tracking slice token watermarks.
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
    if not state_path.exists() and not _legacy_migration_marker_path(state_path).exists():
        # The legacy flat file: read it once and write it in the new format.
        # It is left on disk afterwards, so a reverted sidecar still finds
        # it, and the marker records the read so that a deleted log is not
        # refilled with the legacy baseline.
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
