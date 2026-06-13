# CLAUDE.md - Runway (AI Usage Tracker)

Runway is a local-first monitoring tool for AI provider quotas with SQLite-backed history.

## Architecture
- **Two Topologies**: Local (server + sidecar on same host) and Multi-Host/Docker (server + one or more remote sidecars). The server never performs local detection itself — all LSP probes, browser cookies, and IDE/file introspection live in the sidecar.
- **Docker Rule**: No native desktop UI/keychains in the server container — credentials come from ENV vars or sidecar payloads.
- **Cookie Collectors**: Claude, ChatGPT, Ollama, Kimi Coding, OpenCode need browser cookies; the sidecar extracts them and ships them to the server.

## Commands
A `Makefile` wraps all common tasks — run `make help` for the full list. Key targets:
- **Setup**: `make install` (also wires up the pre-push hook that runs lint + tests)
- **Dev server**: `make dev` (port 8765, hot reload). `make dev-all` runs the server and sidecar together.
- **Production**: `make run` (no reload).
- **Tests**: `make test` (ignores `test_browser_cookies.py` — macOS-only crypto, fails on Linux/WSL). `make test-cov` for coverage.
- **Single test**: `pytest tests/path/to/test_file.py`
- **Lint**: `make lint` (ruff + mypy + pip-audit). `make format` to auto-fix.
- **Frontend**: `make web` (build the SPA into `webapp/dist` — what the server serves), `make web-dev` (Vite dev server on :5173, proxies `/api` to :8765; override target via `RUNWAY_API_URL`), `make web-test` (vitest).
- **Sidecar**: `make sidecar` (sources `.env` so `RUNWAY_CONFIG_DIR` + `INGEST_API_KEY` align with the dev server).
- **Secrets**: `make secrets` (gates — fails on any unbaselined credential in a tracked file; same check as CI). `make secrets-baseline` regenerates the baseline after vetting new detections.

## Schema Fields
When adding new card fields, update `LimitCard` in `app/models/schemas.py`, the mirror in `webapp/src/api/types.ts`, and the README.md TypeScript interface. Token breakdown fields:
- `token_usage`: dict with input/output/reasoning/cache_read/total
- `by_model`: dict with per-model cost/msgs/tokens
- `msgs`: int
- `pct_used`: float

## Data Model
Runway is **event-sourced**. The authoritative table is `usage_events` — one row per assistant message — and everything else is a derived view. All models live in `app/models/db.py`.

| Table | Role |
|-------|------|
| `usage_events` | Per-message immutable events. Deduped by `(provider_id, account_id, event_id)`. `kind="message"` for billable activity, `kind="error"` for provider failures. |
| `usage_period_rollup` | Pre-aggregated rollups (hour/day/month/year/lifetime × model × sidecar grain). Updated incrementally on each event ingest. |
| `usage_windows` | Closed-window archive — totals frozen at each authoritative `reset_at` boundary by `app/services/window_closer.py`. |
| `latest_usage` | Live gauge cards (`pct_used`, `limit_value`, `reset_at`) — what scrapers see. Merged via `merge_card_json` in `app/services/accumulator.py`. |
| `quota_snapshots` | Append-only time-series of `pct_used`/`reset_at` observations. Written on every `upsert_latest_usage` call when `pct_used` is non-null. Backs the `%` history chart and the Theil-Sen forecast. |
| `provider_pricing` | Time-versioned per-(provider, model) prices used by `app/services/cost_calculator.py` so historical cost stays stable across price changes. |
| `provider_configs` | Per-provider user config — API keys, session cookies (Fernet-encrypted), account labels, poll intervals, per-strategy enable toggles. Unique on `(provider_id, account_id)`. |
| `sidecar_registry` | Known sidecars with hostname, custom name, tags, last-seen, version, OS, recent log lines, and a `collection_enabled` pause flag. |
| `webhook_configs` | Discord/Slack threshold alerts: `provider_id`, `threshold_pct`, `url`, `channel`, last-fired timestamp. |
| `system_config` | Single-row global config — browser preference, default poll interval, dashboard layout JSON, user timezone. |
| `audit_log` | Append-only record of admin mutations (sidecar pause/resume/delete/patch, etc.). Diagnostic, not legal-grade. |

**Ingest path:** Sidecar batches up to 1000 events per push to `POST /api/v1/fleet/ingest` (HMAC-signed, rate-limited to 600/min per source IP). Server runs `EventIngestor`, which deduplicates by `event_id`, computes cost via `cost_calculator`, updates rollups, and triggers `window_closer._maybe_close_previous_window` on quota-window boundaries.

**Read paths:**
- Quota cards: `/api/v1/usage/{limits,fleet,cumulative}`. `/usage/fleet` adds `window_aggregations.longest` — per-model + per-sidecar splits aligned to the provider's longest active window (Claude weekly, Gemini daily, etc.) computed on demand from `usage_events`.
- History: `/api/v1/usage/history/{windows,snapshots,chart,window-detail,deltas}` and the legacy `/usage/{window-history,events,heatmap,sessions}`. Snapshot bucketing runs SQL-side via the `ix_quota_snapshots_series_ts` covering index.
- Forecasts: `/api/v1/usage/forecast` (Theil-Sen regression on `quota_snapshots`, anchor-at-now; `include_series=true` returns the drill-down points) and `/api/v1/usage/cost-forecast` (MTD + 7-day burn to EOM).
- Diagnostics: `/api/v1/usage/anomalies` (z-score spike detection) and `/api/v1/system/debug/raw/{provider_id}`.

**Mutating endpoints:** `POST /api/v1/usage/{reset/{provider},collect/{provider}}`, the `/api/v1/fleet/sidecars/{id}/{pause,resume}` controls, the `/api/v1/system/{cleanup,wake,force-collect}` maintenance triplet, and the webhook/provider-config/app-config/dashboard-layout CRUD on `/api/v1/system/` — admin writes go through `require_admin_key` and append to `audit_log`.

**Account identity:** `app/services/account_identity.py:resolve_account_id` — email > UUID > SHA256 hash > `"default"`. Sidecar identity is the hostname; never part of unique constraints on the canonical (`provider_id`, `account_id`) pair.

**Multi-host startup gates:** when `APP_HOST != 127.0.0.1`, the server refuses to start without `DB_ENCRYPTION_KEY`, `TLS_TERMINATED=1`, and an explicit `CORS_ORIGINS` allow-list — sidecar payloads carry tokens, and HMAC isn't confidentiality. See `docs/SECURITY.md`.

## CI/CD
Three workflows live in `.github/workflows/`:

- **`ci-cd.yml`** — runs on push/PR to `main` and version tags (`v*`):
  - **lint-python**: ruff, mypy, detect-secrets, pip-audit
  - **lint-docker**: hadolint
  - **frontend-check**: webapp typecheck + Vite build + vitest (reusable ci-node workflow, working-directory `webapp/`)
  - **test**: pytest with coverage uploaded to Codecov
  - **build-and-push**: Docker image to GHCR (tags only)
- **`release-please.yml`** — opens / merges release PRs from Conventional Commits (see *Releases* below).
- **`sidecar-release.yml`** — on version tags, builds the standalone sidecar binary with PyInstaller and attaches `Runway-Sidecar-macOS-<version>.zip` and `Runway-Sidecar-Windows-<version>.zip` to the GitHub release.

Dependabot updates actions, pip, and npm weekly. Secrets baseline (`.secrets.baseline`) is tracked in git — required by CI.

## Releases
Releases are managed by **Release Please** (`.github/workflows/release-please.yml`):
- Uses Conventional Commits to determine version bumps: `feat:` → minor, `fix:` → patch, `feat!:` → major, `chore:`/`docs:`/`test:` → no release
- On qualifying commits to `main`, Release Please opens a PR updating `CHANGELOG.md` and `package.json`
- Merging that PR creates the GitHub Release and tag automatically
- To force a version jump (e.g. v1.0.0): tag manually, push the tag, create the GitHub Release by hand — Release Please picks up from there

## Code Style & Patterns
- **Backend**: Python 3.12+, FastAPI, Pydantic v2, `httpx` (async).
- **Frontend**: React 19 + TypeScript SPA in `webapp/` (Vite, Tailwind CSS v4, TanStack Query, Radix primitives, ECharts). Dark-first semantic tokens live in `webapp/src/styles/tokens.css` — components use token utilities (`bg-surface-1`, `text-fg-muted`), never raw hex.
- **Async Required**: Everything from endpoints to collectors MUST be `async`.
- **Typing**: Explicit type hints on all API responses and internal models.
- **Error Handling**: Graceful degradation — return "Error Card" states instead of crashing.
- **Surgical Precision**: Preserve existing comments and structure during edits.
- **Sidecar Focus**: Sidecars only extract/forward raw data; main server does the heavy lifting.

## Standard Definitions

### `data_source` (Origin of Payload)
- **`api`**: Official API / OAuth endpoints.
- **`web`**: Unofficial / Cookie-based / Scraped web endpoints.
- **`local`**: Local log files / CLI statuslines / Fast path caches.

### `input_source` (Origin of Credentials)
- **`config`**: Entered via the Runway Dashboard UI (stored in DB).
- **`server`**: Discovered by the local machine (ENV, local files, browser scraping).
- **`sidecar`**: Discovered by a remote agent and pushed to the server.

## Collector Strategy Patterns

Strategies are categorized by data type, collected in phases, and merged into a single card per provider. Anything `local` (CLI / statusline / log scraping) executes inside the sidecar, not the server — server-side collectors only do `api` and `web`. The sidecar pushes its findings via `/api/v1/fleet/ingest`, which `EventIngestor` and the accumulator merge into `usage_events` / `latest_usage`.

| Type | Strategies | Where it runs | Provides |
|------|------------|---------------|----------|
| **quota** | api, web | server | Percentages, currency limits, tier |
| **enrichment** | local (cli / statusline / logs) | sidecar | Token breakdown, session counts, per-message events |

Collection pipeline: server quota collection → merge with sidecar-ingested enrichment → single card per `(provider_id, account_id, window_type, variant, model_id)` tuple in `latest_usage`. See `docs/collection_logic.md` for the full bucket model and `_merge_enrichment` semantics.
