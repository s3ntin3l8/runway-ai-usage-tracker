# CLAUDE.md - Runway (AI Usage Tracker)

Runway is a local-first monitoring tool for AI provider quotas with SQLite-backed history.

## Architecture
- **Two Topologies**: Local (server + sidecar on same host) and Multi-Host/Docker (server + one or more remote sidecars). The server never performs local detection itself — all LSP probes, browser cookies, and IDE/file introspection live in the sidecar.
- **Docker Rule**: No native desktop UI/keychains in the server container — credentials come from ENV vars or sidecar payloads.
- **Cookie Collectors**: Claude, ChatGPT, Ollama, Kimi Coding, OpenCode need browser cookies; the sidecar extracts them and ships them to the server.

## Commands
A `Makefile` wraps all common tasks — run `make help` for the full list. Key targets:
- **Setup**: `make install` (also wires up the pre-push hook that runs lint + tests)
- **Dev server**: `make dev` (port 8765, hot reload)
- **Tests**: `make test` (ignores `test_browser_cookies.py` — macOS-only crypto, fails on Linux/WSL)
- **Single test**: `pytest tests/path/to/test_file.py`
- **Lint**: `make lint` (ruff + mypy + pip-audit)
- **Sidecar**: `make sidecar`

## Schema Fields
When adding new card fields, update both `LimitCard` in `app/models/schemas.py` and the README.md TypeScript interface. Token breakdown fields:
- `token_usage`: dict with input/output/reasoning/cache_read/total
- `by_model`: dict with per-model cost/msgs/tokens
- `msgs`: int
- `pct_used`: float

## Data Model
Runway is **event-sourced**. The authoritative table is `usage_events` — one row per assistant message — and everything else is a derived view.

| Table | Role |
|-------|------|
| `usage_events` | Per-message immutable events. Deduped by `(provider_id, account_id, event_id)`. `kind="message"` for billable activity, `kind="error"` for provider failures. |
| `usage_period_rollup` | Pre-aggregated rollups (hour/day/month/year/lifetime × model × sidecar grain). Updated incrementally on each event ingest. |
| `usage_windows` | Closed-window archive — totals frozen at each authoritative `reset_at` boundary by `app/services/window_closer.py`. |
| `latest_usage` | Live gauge cards (`pct_used`, `limit_value`, `reset_at`) — what scrapers see. Merged via `merge_card_json` in `app/services/accumulator.py`. |
| `provider_pricing` | Time-versioned per-(provider, model) prices used by `app/services/cost_calculator.py` so historical cost stays stable across price changes. |

**Ingest path:** Sidecar batches up to 1000 events per push to `POST /api/v1/fleet/ingest` (HMAC-signed). Server runs `EventIngestor`, which deduplicates by `event_id`, computes cost via `cost_calculator`, updates rollups, and triggers `window_closer._maybe_close_previous_window` on quota-window boundaries.

**Read paths:** `/api/v1/usage/{cumulative,events,window-history,heatmap,sessions,cost-forecast,anomalies}` query rollups + events. `/api/v1/usage/fleet` returns live cards plus `window_aggregations.longest` — per-model + per-sidecar splits aligned to the provider's longest active window (Claude weekly, Gemini daily, etc.) computed on demand from `usage_events`.

**Account identity:** `app/services/account_identity.py:resolve_account_id` — email > UUID > SHA256 hash > `"default"`. Sidecar identity is the hostname; never part of unique constraints.

## CI/CD
Pipeline runs on push/PR to `main` and on version tags (`v*`):
- **lint-python**: ruff, mypy, detect-secrets, pip-audit
- **lint-docker**: hadolint
- **frontend-check**: Tailwind CSS build
- **test**: pytest with coverage uploaded to Codecov
- **build-and-push**: Docker image to GHCR (tags only)

Dependabot updates actions, pip, and npm weekly. Secrets baseline (`.secrets.baseline`) is tracked in git — required by CI.

## Releases
Releases are managed by **Release Please** (`.github/workflows/release-please.yml`):
- Uses Conventional Commits to determine version bumps: `feat:` → minor, `fix:` → patch, `feat!:` → major, `chore:`/`docs:`/`test:` → no release
- On qualifying commits to `main`, Release Please opens a PR updating `CHANGELOG.md` and `package.json`
- Merging that PR creates the GitHub Release and tag automatically
- To force a version jump (e.g. v1.0.0): tag manually, push the tag, create the GitHub Release by hand — Release Please picks up from there

## Code Style & Patterns
- **Backend**: Python 3.12+, FastAPI, Pydantic v2, `httpx` (async).
- **Frontend**: Vanilla CSS (aviation HUD) + Tailwind CSS v4.
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

Strategies are categorized by data type, collected in phases, and merged into a single card per provider:

| Type | Strategies | Provides |
|------|------------|----------|
| **quota** | api, web, sidecar | Percentages, currency limits, tier |
| **mixed** | cli, statusline | Quota + session tokens (split during collection) |
| **enrichment** | local | Token breakdown, session counts |

Collection pipeline: quota → mixed extraction → enrichment → merge into single card.
