---
name: runway
description: Preserve Runway's cross-component correctness contracts when implementing, debugging, refactoring, testing, or reviewing collectors, sidecar ingestion, usage accounting, credentials, and database changes.
---
<!-- mullion:scaffold:runway -->

# Runway correctness contracts

Use the contracts relevant to the change. Follow their code references into callers and tests before changing behavior; these are dependencies between components, not standalone style preferences.

## 1. Event identity and attribution are separate

`usage_events` has a unique index on **`(provider_id, event_id)`**, independent of account and host. Adding `account_id` or `sidecar_id` to the effective deduplication key counts replayed or shared transcripts twice.

In `app/services/event_ingestor.py`, a duplicate may be reattributed only when its original sidecar reports it under a different account. Another host's replay must not change the original attribution. Reattribution refreshes the payload and subtracts the stored message's rollup contribution before adding the refreshed contribution to the new account.

Preserve per-event savepoints for duplicate inserts and the outer transaction covering new events and rollups. A duplicate must not roll back earlier valid events; a genuine batch failure must not leave events and rollups disagreeing.

Evidence: `app/models/db.py::UsageEvent`, `EventIngestor._try_insert_event`, `EventIngestor._reattribute`, `tests/unit/test_event_ingestor.py`, `tests/unit/test_event_identity.py`.

## 2. Repairs must change authoritative events and rebuild their dependents

`usage_events` is authoritative. Editing a rollup or usage window alone creates a repair that the next rebuild can undo. Data Health actions and CLI repair wrappers share plan/apply services under `app/services/maintenance/`; keep the substantive repair there.

For repairs that commit batches, failure after an earlier batch still leaves committed event changes. Preserve rebuilding for the affected provider/account pairs even on partial failure. `maintenance/rebuild.py::rebuild_pairs_best_effort` rolls back the failed transaction before rebuilding and does not mask the original exception.

Evidence: `app/services/period_rollups.py`, `app/services/maintenance/rebuild.py`, `docs/data-health.md`.

## 3. An empty response is not proof that live cards disappeared

Live cards are materialized from producer contributions. Only a fresh, successful, complete provider snapshot may retire that producer's omitted cards. Cached, skipped, failed, and partial cycles retain last-good contributions; sustained failure ages them to stale after an hour. An account disappearing from the server's active collector set is not a successful empty snapshot.

`BaseCollector.complete_snapshot` defaults conservatively; `COMPLETE_SNAPSHOT = True` is an explicit promise. Valid empty results also need `successful_empty_result`. On the sidecar path, `completed_providers` carries the corresponding promise. Do not mark a provider complete merely because its collection attempt finished.

Reconcile contributions within their producer scope. One host's empty snapshot must not remove another producer's contribution or historical events/snapshots. A fresh server contribution owns the materialized slot as `local`; otherwise the freshest healthy sidecar owns it, falling back to the most recently updated source when all are stale.

Evidence: `app/services/collectors/base.py`, `app/services/accumulator.py`, `scripts/sidecar.py`, `docs/collection_logic.md` → Current-card reconciliation.

## 4. Strategy success and enrichment have different authority

Primary strategies run in declaration order: first success wins. Options containing `enrich: True` designate enrichment, which runs only after primary success and cannot rescue failed quota collection.

`BaseCollector._enrich_results` replaces `token_usage`, `by_model`, and `msgs`; it does not sum or maximize them. Quota percentage remains derived from the primary card's `used_value / limit_value`. Preserve model identity through every fallback key: a model-specific card must not receive aggregate enrichment or another model's enrichment.

The sidecar is a separate producer, not another primary strategy. Per-message breakdowns from shipped collectors arrive as usage events.

Evidence: `app/services/collectors/base.py::collect`, `BaseCollector._enrich_results`, `docs/collection_logic.md`. For model matching, inspect the implementation: it explicitly refuses specific-model-to-aggregate fallback.

## 5. Local discovery cannot migrate into server collectors

The server can run in Docker without desktop facilities or the client's home directory. Filesystem/IDE inspection, browser cookies, CLI caches, LSP probes, and local commands belong in the sidecar. Server collectors consume credentials supplied through Settings, environment configuration, or sidecars.

The credential-file exception is Runway's own `github_oauth.json` in its config directory. Preserve `_server_may_read` before path expansion; do not make generic registry file rules readable by the server. Collector taxonomy must continue to distinguish `data_source` (`api`, `web`, `local`) from `input_source` (`config`, `server`, `sidecar`).

Registry changes also affect the standalone sidecar: regenerate its baked rules with `make sidecar-registry`, rather than editing the generated block.

Evidence: `app/services/credential_provider.py::_server_may_read`, `app/core/registry.json`, `tests/unit/test_server_reads_no_credential_files.py`.

## 6. Credential expiry must mean the same thing on both sides

The server and standalone sidecar independently normalize expiry timestamps. Keep `app/core/expiry.py::expiry_seconds` and `scripts/sidecar_pkg/expiry.py::expiry_seconds` equivalent: seconds below `1e11`, milliseconds from `1e11`, microseconds from `1e14`, nanoseconds from `1e17`. Non-numbers, non-finite values, and normalized values beyond year 9999 mean unknown expiry, not freshness.

When selecting among wildcard-matched credential records, the token and expiry must come from the same record. Unknown expiry must not outrank a known expiry. Dividing every value by 1000 once makes microsecond credentials look fresh for thousands of years.

Evidence: `tests/unit/test_mapping_resolver_parity.py`, `docs/collection_logic.md` → Expiry units.

## 7. Reported cost, estimated value, and cache dimensions are not interchangeable

Live ingest and offline recost share `app/services/maintenance/event_cost.py::resolve_event_cost`. Preserve its decision order: a reported amount wins for pay-as-you-go billing; it also wins when no pricing row resolves; otherwise use the computed estimate. A zero-rate pricing row is different from a missing row. Subscription estimates are API-equivalent usage value, not proof of a bill.

Pass the full cache-create total and its 1h/5m split through both paths. Repricing without the split silently replaces correct 1h prices with 5m prices. Aggregate cloud remainder events use `standard_tier_only`; their accumulated tokens must not trigger a per-request long-prompt tier.

Free-tier model IDs must not fall through model-name trimming to a paid sibling rate.

Evidence: `app/services/maintenance/event_cost.py`, `app/services/cost_calculator.py`, `tests/unit/test_event_ingestor.py`, `tests/unit/test_cost_calculator.py`.

## 8. Provider, client app, project, and session name encode different identities

`provider_id` identifies the billed provider; `source_app` identifies the client that made the call. Preserve `source_app` through provider retags. Older inputs fall back to their incoming provider ID.

Client working directories are foreign paths: use `app/services/project_label.py`, which understands both separators, rather than the server's `os.path`. Prefer a cleaned `project_ref` repository name over a folder label. The sidecar resolves repository identity; remote URLs must not leave the client because they can contain credentials.

Whole-session shallowest-directory consolidation belongs in maintenance, not single-event ingest, and must not collapse unrelated projects under shared ancestors.

Session names are explicit titles, not conversation snippets. Codex `title`, `preview`, and `first_user_message`, and Antigravity `preview`, must not become fallback names. Names are never logged. The sidecar commits sent-name hashes only after the first batch acknowledges `names_upserted` without `names_error`; transport success alone, or an older server ignoring the field, is insufficient.

Evidence: `app/services/event_ingestor.py`, `app/services/project_label.py`, `scripts/sidecar_pkg/git_identity.py`, `scripts/sidecar_pkg/session_names.py`, `scripts/sidecar.py`, `docs/collection_logic.md`.

## 9. A model declaration does not upgrade an installed database or API consumer

`SQLModel.metadata.create_all` does not add columns to existing SQLite tables. Schema additions also need the idempotent startup migrations in `app/core/db.py`; exercise both fresh and existing database shapes.

A new `LimitCard` field crosses three maintained surfaces: `app/models/schemas.py`, `webapp/src/api/types/usage.ts`, and the interface example in `docs/api-reference.md`.

Evidence: `app/core/db.py::init_db`, its column-addition helpers, and AGENTS.md → Core invariants.
