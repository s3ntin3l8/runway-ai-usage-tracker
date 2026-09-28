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
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow importing from app/ when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.models.schemas import UsageEventPush  # noqa: E402

logger = logging.getLogger("runway.sidecar.hermes")

# TODO: Hoist shared provider mappings to scripts/sidecar_pkg/canonical_providers.py
# Upstream billing_provider -> (canonical provider_id, explicit account override or None).
_HERMES_CANONICAL_MAP: dict[str, tuple[str, str | None]] = {
    "kimi-coding": ("kimi_coding", None),
    "minimax": ("minimax", None),
    "minimax-oauth": ("minimax", None),
    "minimax-coding-plan": ("minimax", None),
    "opencode-go": ("opencode", None),
    "opencode-zen": ("opencode", None),
    "opencode": ("opencode", None),
    "openrouter": ("openrouter", None),
    "deepseek": ("deepseek", None),
    "deepseek-api": ("deepseek", None),
    "anthropic": ("anthropic", None),
    "gemini": ("gemini", None),
    "ollama": ("ollama", None),
    "ollama-cloud": ("ollama", None),
    "xai": ("xai", None),
}


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


def _discover_hermes_db_paths() -> list[Path]:
    """Discover all Hermes state.db files: default profile and named profiles."""
    paths: list[Path] = []

    # 1. HERMES_HOME env override
    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        p = Path(hermes_home).expanduser() / "state.db"
        if p.exists():
            paths.append(p)

    # 2. Standard default profile
    default_db = Path(os.path.expanduser("~/.hermes/state.db"))
    if default_db.exists() and default_db not in paths:
        paths.append(default_db)

    # 3. Named profiles in ~/.hermes/profiles/*/state.db
    profiles_dir = Path(os.path.expanduser("~/.hermes/profiles"))
    if profiles_dir.is_dir():
        for prof_db in sorted(profiles_dir.glob("*/state.db")):
            if prof_db.exists() and prof_db not in paths:
                paths.append(prof_db)

    return paths


def _default_watermark_state_path() -> Path:
    config_dir = os.getenv("RUNWAY_CONFIG_DIR")
    if config_dir:
        base = Path(config_dir) / "sidecar"
    else:
        base = Path(os.path.expanduser("~/.config/runway/sidecar"))
    return base / "hermes_watermark.json"


def _load_hermes_watermark(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            logger.warning("Hermes watermark file %s is empty, ignoring", path)
            return {}
        data = json.loads(content)
        if isinstance(data, dict):
            return data
        logger.warning("Hermes watermark at %s did not contain a JSON object", path)
    except Exception as exc:
        logger.warning("Failed to read Hermes watermark state from %s: %s", path, exc)
    return {}


def _save_hermes_watermark(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("Failed to save Hermes watermark state to %s: %s", path, exc)


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
    # TODO(multi-account): The slice-level watermark (hermes_watermark.json) is
    # currently shared across all accounts on the host. Today Hermes has a single host
    # identity (HERMES_ACCOUNT_LABEL), but if multi-account Hermes support is added
    # (e.g. server's scoped_accounts loop iterating multiple accounts per cycle),
    # callers should supply an account-scoped state_file or include account_id in
    # state_key so later accounts do not find the watermark already advanced.
    state_path = state_file or _default_watermark_state_path()
    watermark_state = _load_hermes_watermark(state_path)
    state_modified = False

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
                        COALESCE(s.profile_name, ?) as profile_name
                    FROM session_model_usage smu
                    LEFT JOIN sessions s ON smu.session_id = s.id
                    WHERE COALESCE(smu.last_seen, smu.first_seen) > ?
                       OR (smu.last_seen IS NULL AND smu.first_seen IS NULL)
                    ORDER BY COALESCE(smu.last_seen, smu.first_seen, 0) ASC
                """
                rows = cur.execute(query, (profile_name, since_epoch)).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("Failed to query Hermes state DB at %s: %s", db_path, exc)
            continue

        for row in rows:
            session_id = row["session_id"]
            model = row["model"] or "unknown"
            billing_provider = row["billing_provider"] or ""
            task = row["task"] or ""

            raw_last_seen = row["last_seen"]
            raw_first_seen = row["first_seen"]
            if raw_last_seen is None and raw_first_seen is None:
                logger.warning(
                    "Hermes session_model_usage row for session %s has both last_seen and first_seen NULL; skipping",
                    session_id,
                )
                continue

            last_seen = float(raw_last_seen if raw_last_seen is not None else raw_first_seen)
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
            curr_cost = float(actual_cost if actual_cost is not None else (est_cost or 0.0))

            # Watermark key uniquely identifies this slice within the DB; resolve path to avoid collisions across roots
            state_key = (
                f"{db_path.resolve()}|{profile_name}|{session_id}|{model}|{billing_provider}|{task}"
            )
            prev = watermark_state.get(state_key, {})

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
            canonical = map_hermes_canonical(billing_provider)
            if canonical is not None:
                canonical_provider_id, account_override = canonical
                target_provider_id = canonical_provider_id

                if account_override is not None:
                    event_account_id = account_override
                    event_account_source = "tag"
                elif canonical_hints:
                    provider_hints = canonical_hints.get(canonical_provider_id, {})
                    canonical_hint = provider_hints.get(f"provider:{canonical_provider_id}")
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
                target_provider_id = map_hermes_provider_id(billing_provider)
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
                    account_id=event_account_id,
                    account_source=event_account_source,
                    event_id=event_id,
                    ts=ts.isoformat(),
                    model_id=model,
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
                    entrypoint="hermes",
                    kind="message",
                )
            )

            # Update tracked watermark monotonically
            watermark_state[state_key] = {
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
            state_modified = True

    if state_modified:
        _save_hermes_watermark(state_path, watermark_state)

    return events
