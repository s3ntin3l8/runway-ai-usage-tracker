# Phase 4.5 — Pre-Release Hardening

**Date:** 2026-04-13
**Status:** Approved for implementation

## Context

Phases 0–4 are complete. Three 1.0 release prerequisites remain open before Phase 5 work begins. These are independent, low-risk items that harden the runtime before the Phase 5 polish features land.

---

## 1A: Pydantic Settings

**Files:** `app/core/config.py`, `requirements.txt`

Replace the plain `Settings` class (using `os.getenv()` calls) with `pydantic_settings.BaseSettings`.

- Add `pydantic-settings` to `requirements.txt`
- `Settings` extends `BaseSettings`; every field gets a type annotation and default
- Env var name inferred from field name (uppercase); use `alias` only where the env var name differs from the field name
- Platform path helpers (`get_platform_data_dir()`, `get_platform_config_dir()`) stay as standalone module-level functions — they're OS logic, not config
- The `settings` singleton and all import sites unchanged — zero change to consumers
- Validation errors at startup (missing required env vars) raise `ValidationError` with a clear message instead of silently returning `None`

---

## 1B: Structured Logging

**Files:** `app/core/logging.py` (new), `app/main.py`, `app/core/config.py`

Add a `LOG_FORMAT` env var (`plain` | `json`, default `plain`).

- New `app/core/logging.py` with a `JsonFormatter(logging.Formatter)` class (~20 lines)
  - Emits one JSON object per line: `{"timestamp": ISO8601, "level": "INFO", "logger": "app.main", "message": "..."}`
  - `exc_info` key is included only when an exception is attached to the log record; contains the formatted traceback string
- In `main.py` startup: if `settings.log_format == "json"`, replace the root handler's formatter with `JsonFormatter()`
- `httpx` silencing at WARNING level stays regardless of format

---

## 1C: Data Retention Compaction

**Files:** `app/services/compaction.py` (new), `app/services/poller.py`

Background job to downsample old snapshots. Thresholds: **60 days raw**, **60–180 days hourly averages**, **180+ days daily averages**.

- New `app/services/compaction.py` with `compact_snapshots(session: Session) -> dict` returning `{"hourly_compacted": N, "daily_compacted": N}`
- **Hourly pass (60–180 days):**
  - Group by `(provider_id, account_id, model_id, window_type, strftime('%Y-%m-%d %H', timestamp))`
  - For each group with >1 row: average `used_value` / `limit_value`, delete originals, insert one compacted row with `raw_metadata = NULL`
- **Daily pass (180+ days):**
  - Same but group by `strftime('%Y-%m-%d', timestamp)`
- **Poller integration:** Add `_poll_count` counter to `BackgroundPoller`. Every 96 polls (15 min × 96 = 24 h), call `compact_snapshots()` inside the existing poll loop.
- Compacted rows have `raw_metadata IS NULL` — the function never re-compacts already-compacted rows

---

## Verification

- `pytest` — all existing tests pass after Settings migration
- Set `LOG_FORMAT=json`, start the app, confirm stdout emits valid JSON lines
- Manually insert rows with old timestamps, call `compact_snapshots()`, confirm rows are downsampled and `raw_metadata` is NULL
- Confirm `settings.field_name` resolves correctly across all import sites
