# Runway — Roadmap to 1.0

This document defines the implementation roadmap for Runway's first stable release. Items are organized into sequential phases by dependency order and grouped by implementation priority.

> **Pre-1.0 Policy:** We have not shipped a public release. All schema, protocol, and API changes can be made freely without migration scaffolding. Optimize for correctness and long-term ergonomics, not backward compatibility.

---

## Phase 0 — Schema Foundation

These are strict prerequisites for everything else. No DB code, no history UI, no fleet management until both are complete.

### 0A. Unified `LimitCardBuilder` Pattern
**Effort:** Small | **Status:** ✅ Complete (2026-04-12)

Recent 500 errors were caused by manual dictionary construction missing mandatory Pydantic fields (`icon`, `reset`, `pace`).

*   **Builder Implementation:** Replace direct dictionary returns in collectors with a `LimitCardBuilder` that enforces schema consistency and provides intelligent defaults based on the provider health state.
*   **Benefit:** Structural safety net — prevents malformed cards from poisoning the DB at write time and eliminates an entire class of runtime errors across all collectors.

---

### 0B. Clean Schema & Metadata Promotion
**Effort:** Medium | **Status:** ✅ Complete (2026-04-12)

Move high-value metadata currently "hidden" in the unstructured `detail` string or the `metadata` dict into dedicated, top-level fields on `LimitCard` and `IngestRequest`.

#### `LimitCard` — New Top-Level Fields

| Field | Type | Description |
|:---|:---|:---|
| `provider_id` | `str` | Platform identifier (e.g. `"anthropic"`, `"openai"`). Currently fished from `metadata` dict in `ingest.py` — must be a first-class field. |
| `account_id` | `Optional[str]` | Unique account hash/ID. Currently injected by `base.py._tag_results()` into the raw dict but not part of the Pydantic model. |
| `account_label` | `Optional[str]` | Human-readable identity (email, org name). Replaces the current `account_name` key. |
| `model_id` | `Optional[str]` | Specific model identifier. Currently buried in `detail` display strings. **Convention:** `None` = aggregate/account-level snapshot; specific value = per-model breakdown. |
| `sidecar_id` | `Optional[str]` | Originating host FQDN or tag. `None` = local collection. |
| `window_type` | `str` | Reset window classification. **Enum:** `daily`, `weekly`, `monthly`, `session`, `rolling`, `unknown`. Default: `"unknown"`. |

#### `IngestRequest` — New Fields

| Field | Type | Description |
|:---|:---|:---|
| `sidecar_id` | `str` | Originating host identifier. Required — every sidecar must self-identify. The server propagates this to all ingested `LimitCard` objects and the DB. |

> `sidecar_id` is business data (which machine produced this metric), not a transport concern. It belongs in the request body where it's validated by Pydantic, visible in the OpenAPI schema, and self-documenting — not in an HTTP header.

#### Naming Standardization

| Current | Target | Notes |
|:---|:---|:---|
| `LimitCard.service` | `LimitCard.service_name` | Align with DB column. One rename, applied everywhere. |
| `account_name` (ad-hoc dict key) | `account_label` (Pydantic field) | Graduated from untyped dict to validated field. |

#### Target Fields from Detail String Promotion

*   `model_id`: Promote specific model IDs from `Model: ...` strings.
*   `tier`: Already exists on `LimitCard` ✅. Ensure all collectors populate it.
*   `data_source`: Already exists ✅. Standardize to drive UI icons (OAuth → 🔑, Web → 🌐, Logs → 📄).

**Benefits:** Enables granular filtering (e.g., "show all Pro accounts"), cleaner UI rendering without string parsing, consistent visual badging, and a clean write path to SQLite.

---

## Phase 0.5 — Hardening ✅ Complete (2026-04-12)

Fixes identified during the pre-1.0 codebase audit. Mostly trivial-effort items that improve security, stability, and performance. Can be executed in parallel with Phase 0 work.

> Full audit details: see `codebase_audit.md` in project artifacts.

### Security Fixes ✅ Complete (2026-04-12)

| ID | Fix | Effort | File |
|:---|:---|:---|:---|
| **S1** | Remove `str(exc)` from global exception handler response body — leaks internals | Trivial | `main.py` |
| **S2** | Add rate limiting (`slowapi`) on `/api/limits` (10/min), `/api/health` (30/min), GitHub OAuth (5/min) | Small | `main.py`, new middleware |
| **S3** | Strip `/api/health` response to non-sensitive summary, or gate behind API key | Trivial | `health.py` |
| **S4** | Replace raw `open()` with `safe_write_json()` in GitHub token save | Trivial | `github_oauth.py` |
| **S5** | Replace raw `open()` with `safe_write_json()` in ChatGPT token persistence | Trivial | `chatgpt.py` |
| **S6** | Make `CORS_ORIGINS` configurable via env var, derive defaults from `APP_HOST`/`APP_PORT` | Small | `config.py` |

### Stability Fixes ✅ Complete (2026-04-12)

| ID | Fix | Effort | File |
|:---|:---|:---|:---|
| **T1** | Eliminate dual `CollectorManager` instances — use single global from `collector_manager.py` in `routes.py` | Trivial | `routes.py`, `collector_manager.py` |
| **T2** | Remove internal caching from ChatGPT and Gemini collectors — SmartCollector is the single caching layer | Small | `chatgpt.py`, `gemini.py` |
| **T3** | Add `asyncio.Lock` to `_sync_collectors()` to prevent race-condition duplicate spawning | Trivial | `collector_manager.py` |
| **T4** | Fix stale eviction in `ExternalMetricService.get_all_metrics()` — persist deletion to disk | Trivial | `external_metrics.py` |
| **T5** | Close `httpx.AsyncClient` in FastAPI lifespan shutdown handler | Trivial | `main.py` |
| **T6** | Use `copy.deepcopy()` in `SmartCollector._tag_as_cached()` to protect nested `metadata` dicts | Trivial | `smart_collector.py` |
| **T7** | Change Dockerfile `HEALTHCHECK` from `/api/limits` to `/api/health` (matches docker-compose) | Trivial | `Dockerfile` |

### Performance Fixes ✅ Complete (2026-04-12)

| ID | Fix | Effort | File |
|:---|:---|:---|:---|
| **P1** | Add single-flight pattern to `/api/limits` — concurrent requests coalesce into one collection cycle | Small | `routes.py` or `collector_manager.py` |
| **P2** | Hoist `_get_credentials()` call above the bucket loop in `GeminiCollector._collect_via_api` | Trivial | `gemini.py` |
| **P3** | Throttle `_sync_collectors()` to run at most once per 60 seconds | Trivial | `collector_manager.py` |
| **A3** | Debounce `ExternalMetricService` disk writes to once per 30 seconds (in-memory is source of truth) | Small | `external_metrics.py` |

---

## Phase 1 — Stateful Core ✅ Complete (2026-04-12)

The foundational shift from a purely stateless monitor to a stateful local-first application.

### 1A. SQLite Usage History
**Effort:** Large | **Status:** ✅ Complete (2026-04-12)
**Depends on:** Phase 0 (both 0A and 0B must be complete)

A "Max Variant" schema capturing universal metrics (cost, tokens) and provider-specific JSON metadata for deep-dive trends. Uses **SQLite** and **SQLModel**.

#### Schema: `usage_snapshots`

| Column | Type | Constraints | Example |
|:---|:---|:---|:---|
| `id` | `INTEGER` | Primary Key, autoincrement | `1024` |
| `timestamp` | `DATETIME` | NOT NULL | `2026-04-11 14:30:00` |
| `provider_id` | `TEXT` | NOT NULL | `"anthropic"` |
| `account_id` | `TEXT` | NOT NULL | `"user-abc-123"` |
| `account_label` | `TEXT` | | `"john@work.com"` |
| `service_name` | `TEXT` | NOT NULL | `"Claude Pro"` |
| `used_value` | `REAL` | | `15.50` |
| `limit_value` | `REAL` | | `100.00` |
| `unit_type` | `TEXT` | NOT NULL, default `"generic"` | `"percent"` |
| `currency` | `TEXT` | | `"USD"` |
| `tier` | `TEXT` | | `"Pro"` |
| `model_id` | `TEXT` | Nullable — `NULL` = aggregate snapshot | `"claude-3-5-sonnet"` |
| `window_type` | `TEXT` | NOT NULL, default `"unknown"` | `"session"` |
| `health` | `TEXT` | NOT NULL | `"good"` |
| `sidecar_id` | `TEXT` | Nullable — `NULL` = local collection | `"macbook-pro-1"` |
| `is_unlimited` | `BOOLEAN` | NOT NULL, default `false` | `false` |
| `data_source` | `TEXT` | NOT NULL | `"oauth"` |
| `error_type` | `TEXT` | | `null` |
| `raw_metadata` | `JSON` | Provider-specific escape hatch | `{"latency_ms": 450}` |

#### Indexes

| Index | Columns | Rationale |
|:---|:---|:---|
| `ix_snapshot_lookup` | `(provider_id, account_id, timestamp DESC)` | Primary query pattern: "show me history for this account, newest first." Composite index — three separate indexes would not be combined by SQLite's query planner. |
| `ix_snapshot_time` | `(timestamp)` | Retention pruning queries and time-range scans. |
| `ix_snapshot_sidecar` | `(sidecar_id)` | Fleet management queries: "show all data from this host." |

#### `window_type` Enum Convention

Each provider maps to a known reset window. Collectors must set this explicitly:

| Provider | `window_type` | Notes |
|:---|:---|:---|
| Claude (Free) | `rolling` | Rolling 5-hour token window |
| Claude (Pro/Team) | `monthly` | Monthly billing cycle |
| ChatGPT | `daily` | Daily message caps |
| GitHub Copilot | `monthly` | Monthly billing |
| Ollama | `session` | Local, no reset window |
| Antigravity | `session` | Session-based credits |

#### Data Retention Policy

Unbounded snapshot storage will grow ~1K rows/day (5 providers × 2 accounts × 96 polls). Over a year that's 350K+ rows.

| Age | Resolution | Action |
|:---|:---|:---|
| 0–30 days | Raw (every poll) | Keep as-is |
| 30–90 days | Hourly averages | Background compaction job |
| 90+ days | Daily averages | Background compaction job |

Compaction runs as part of the background polling loop (see Phase 4, #4C). Compacted rows set `raw_metadata = NULL` to reclaim space.

---

### 1B. Settings UI
**Effort:** Medium | **Status:** ✅ Complete (2026-04-12)

*   **Top Navbar:** Dashboard | History | Settings — replaces/augments `.env` configuration.
*   **Machine-Key Encryption:** Securing API keys in the local DB using `cryptography.fernet`.

> **Key management strategy:** Runway reads the encryption key from the `DB_ENCRYPTION_KEY` env var. If unset, encryption is skipped and the DB stores values in plaintext (acceptable for local-only deployments). Document the setup requirement in the README and `.env.example`.
>
> **Migration:** The key travels with the DB — back it up alongside the database file. No CLI tooling needed; it's just an env var.

---

### 1C. Passive Background Polling
**Effort:** Small | **Status:** ✅ Complete (2026-04-12)

A 15-minute background loop ensuring data is captured even when the UI is closed, synchronized via TTL caches. This is the write path that feeds `usage_snapshots`.

---

## Phase 2 — Quick Wins ✅ Complete (2026-04-12)

These items are parallelizable with each other and can be worked on alongside Phase 1. They are low-effort, high-impact improvements.

### 2A. In-UI Credential & Cookie Health Indicators
**Effort:** Small | **Status:** ✅ Complete (2026-04-12)

Surface the credential source and health state directly on each quota card in the dashboard.

*   **Data Source Badge:** A small pill/icon on each card showing where data came from (e.g., `OAuth`, `Safari Cookie`, `ENV`, `Sidecar`, `Cached`) using existing `data_source` field.
*   **Credential Status Indicator:** A `⚠️` badge when the credential is known-expired or approaching expiry.
*   **Browser Fallback Notice:** When Chrome cookie extraction fails due to ABE, surface a subtle inline hint on affected cards (e.g., *"Login detected in Chrome but could not decrypt — try Safari"*).
*   **Implementation:** Purely a frontend rendering enhancement — no new backend needed.

---

### 2B. Collector Health Status Endpoint
**Effort:** Small | **Status:** ✅ Complete (2026-04-12)

`SmartCollector` already implements a `get_stats()` method returning cache age, consecutive error count, last error message, and TTL for every collector. This data is only visible in debug logs.

*   **`GET /api/status`:** Returns `get_stats()` output from all active `SmartCollector` instances, giving operators a live view of which collectors are healthy, degraded (stale cache), or in error backoff.
*   **Zero new logic required** — just wire `CollectorManager.smart_collectors` to the new route.

---

### 2C. OAuth Terminal Failure Reset
**Effort:** Small | **Status:** ✅ Complete (2026-04-12)

In `OAuthBaseCollector`, the `_terminal_failure = True` flag (set on `invalid_grant` responses) permanently disables OAuth refresh for that collector's lifetime. The only recovery path is a server restart.

*   **`POST /api/reset/{provider}`:** Clears `_terminal_failure` and resets the error count on the relevant `SmartCollector`. Accepts an optional `account_id` query parameter to target a specific account in multi-account setups (without it, resets all accounts for the provider).
*   **UI button:** A "Retry Auth" button in the error card tooltip/detail for `auth_failed` error type cards.

---

### 2D. Sidecar Clock Skew Detection
**Effort:** Small | **Status:** ✅ Complete (2026-04-12)

The ingest endpoint silently rejects payloads where `|server_time - sidecar_timestamp| > 300s` with a generic 401. If a remote machine has drifted NTP, the sidecar will fail silently.

*   **Structured error response:** Return HTTP **400** (not 401 — this is a client data issue, not an auth failure) with body `{"detail": "timestamp_expired", "skew_seconds": 312}` so the sidecar can log an actionable clock sync warning.
*   **Sidecar-side warning:** Log `⚠️ Clock skew detected — check NTP sync` when a 400 with `timestamp_expired` is received, rather than a generic auth error.

---

### 2E. Browser Preference Ordering
**Effort:** Small | **Status:** ✅ Complete (2026-04-12)

`get_all_browser_cookies_paths()` returns browsers in a hardcoded order: Safari first on macOS, then Chrome/Chromium/Edge, then Firefox. The first browser with a matching cookie wins, meaning a stale Safari session silently shadows an active Firefox or Chrome session.

*   **`BROWSER_PREFERENCE` env var:** A comma-separated ordered list (e.g., `BROWSER_PREFERENCE=firefox,safari,chrome`) to override the default discovery order per deployment.
*   **Especially useful on macOS** where Safari is default-first but developers may primarily use Firefox.

---

## Phase 3 — Architecture Health ✅ Complete (2026-04-13)

Refactoring and cleanup to reduce technical debt before building advanced features on top.

### 3A. Modular Collector Orchestrators (Mixin Pattern)
**Effort:** Medium | **Status:** ✅ Complete (2026-04-12)

Apply the successful Anthropic refactoring pattern (separating `oauth`, `web`, and `local` into mixins) to ChatGPT and Gemini.

*   **Benefits:** Reduces file complexity, enables unit testing strategies in isolation, and improves maintainability of provider-specific fallback logic.

---

### 3B. Centralized Identity & Metadata Extraction
**Effort:** Small | **Status:** ✅ Complete (2026-04-12)
**Depends on:** 3A (modular refactoring should land first so the extraction logic has clean boundaries)

Collectors currently have duplicated logic for extracting emails from JWT `id_token` payloads or parsing identities from raw detail strings.

*   **`IdentityExtractor` Utility:** A shared helper in `app/core/utils.py` that handles standard JWT decoding (with padding fixes) and regex-based string cleaning.
*   **Consistent Account Naming:** Ensure all collectors promote the same level of identity (e.g., `email @ org`) to the `account_label` field.

### 3C. API Restructuring & v1 Pathing
**Effort:** Medium | **Status:** ✅ Complete (2026-04-13)

Move all existing routes under a `/api/v1/` prefix to establish a stable integration point before the 1.0 release.

*   **Modular Routers:** Split the current monolithic `routes.py` into domain-specific routers (e.g., `usage.py`, `settings.py`, `fleet.py`) using FastAPI's `APIRouter`.
*   **Path Mapping:**
    *   `/api/limits` → `/api/v1/usage/limits`
    *   `/api/history` → `/api/v1/usage/history`
    *   `/api/ingest` → `/api/v1/fleet/ingest`
*   **Benefit:** Enables future versioning (v2, v3) without breaking existing sidecar deployments or external integrations, and improves backend code navigate-ability.

---

## Phase 4 — Platform Evolution ✅ Complete (2026-04-13)

Features that build on the stateful core to deliver the full Runway experience.

### 4A. Context-Aware Dashboard Reorganization
**Effort:** Medium | **Status:** ✅ Complete (2026-04-13)
**Depends on:** Phase 0 (`provider_id` must be a top-level field)

*   **Grouping by Provider (Sectioned Grid):** Cards grouped by `provider_id` under collapsible section headers with provider icons and card counts.
*   **Context Filter Pills:** Dimension picker (Source / Account / Window) above the grid with auto-generated pills from live data. Active filter persists across page reloads via `localStorage`.
*   **Source Badge:** Small circular initial badge on each card showing the originating `sidecar_id`.

---

### 4B. Sidecar Fleet Management
**Effort:** Medium | **Status:** ✅ Complete (2026-04-13)
**Depends on:** Phase 1 (SQLite for config storage), Phase 0 (`sidecar_id` field)

*   **Persistent Registry:** `sidecar_registry` SQLite table auto-registers sidecars on first ingest. Tracks `last_seen`, `first_seen`, `ingest_count`, `last_ip`, `error_count`.
*   **Fleet UI Tab:** Cards per sidecar with activity status dot (green/amber/grey by last-seen recency), tag pills, custom name inline-edit, and delete.
*   **CRUD API:** `GET/PATCH/DELETE /api/v1/fleet/sidecars/{id}` with optional `ADMIN_API_KEY` guard.

---

### 4C. Background Refresh & "Instant-Cache" Serving
**Effort:** Medium | **Status:** ✅ Complete (2026-04-13)
**Depends on:** Phase 1 (SQLite)

*   **In-Memory Registry:** `manager._registry` holds the latest card snapshot. `_do_collect()` is the single authoritative write path — startup, poller, and fallback all update it through `collect_all()`.
*   **Instant Respond:** `GET /api/v1/usage/limits` reads from the registry without ever triggering a collection. Falls back to `collect_all()` only when the registry is empty (first request before startup completes).

---

### 4D. Token Health & Proactive Refresh
**Effort:** Medium | **Status:** ✅ Complete (2026-04-13)

*   **Token Health Panel:** Settings page section listing all cached credentials with `valid` / `expiring` / `expired` / `unknown` status badges and ISO expiry timestamps derived from JWT `exp` claims.
*   **One-Click Refresh:** "REFRESH" button appears when `can_refresh && status !== 'valid'`. Calls `POST /api/v1/system/token-health/refresh/{provider}/{account_id}` — supported for Anthropic and Gemini OAuth.

> Token refresh is only feasible for OAuth tokens. Chrome 127+ enforces App-Bound Encryption, making Chrome cookie renewal impossible. Scope remains OAuth + Safari/Firefox only.

---

## Phase 5 — Polish & Scale

Features that enhance the experience but aren't required for a functional 1.0.

### 5A. Chart.js Visualizations
**Effort:** Medium
**Depends on:** Phase 1 (history data)

Replace static cards with interactive time-series charts showing financial burn rates, token volume trends, and comparative provider usage. Filtering by `model_id`, `provider_id`, `sidecar_id`, and `tier` requires all Phase 0 schema fields to be populated.

> When plotting burn rates, the `window_type` enum is essential for normalizing data across providers with different reset windows (daily vs. monthly vs. rolling).

---

### 5B. Metrics Export & Webhooks
**Effort:** Medium
**Depends on:** Phase 1 (history data)

*   **CSV Expense Reporting:** A "Download CSV" button on the History page for formatted expense reports (tax write-offs, employer reimbursement).
*   **Prometheus:** Add `/api/limits?format=prometheus` for external monitoring integrations.
*   **Webhooks:** Send Discord/Slack alerts when quotas cross critical thresholds (e.g., >90% used).

---

### 5C. Intelligent Polling & Power Efficiency
**Effort:** Medium
**Depends on:** Phase 4C (Background Refresh)

*   **Dynamic Backoff ("Sleep Mode"):** Automatically detect user inactivity and drop polling frequency to once every 2 hours.
    *   **Inactivity definition:** Raw quota percentage unchanged AND no new API calls detected via log/local sources AND `updated_at` timestamp drift exceeds threshold. Checking only raw numbers is insufficient — a stable quota with active usage would falsely trigger sleep mode.
*   **Instant Wake:** Snaps back to high-frequency polling when fresh usage is detected or the UI is opened.

---

### 5D. Native Desktop Sidecar (Binary + Menubar App)
**Effort:** Large

Distribute the sidecar as a zero-dependency desktop application (PyInstaller) with real-time visibility and configuration.

*   **OS-Native Desktop Notifications:** Leverages macOS/Windows notification centers for critical quota limits or expired browser sessions.
*   **Hybrid Operation (GUI & Headless):**
    *   **Desktop Mode:** System tray icon (Windows) or Menubar app (macOS) with a right-click menu showing connection status, last sync, and a "Settings..." option.
    *   **Headless Mode:** Auto-detects environments without a display or uses `--headless` to run as a pure background daemon.
*   **Lightweight Configuration:**
    *   **GUI Settings:** Uses `tkinter` for a zero-dependency native dialog. Must detect `tkinter` availability at runtime and fall back to interactive CLI prompts for headless/Docker environments — `tkinter` is not a guaranteed dependency (broken on macOS system Python, absent in Alpine Docker).
    *   **Unified Config:** Reads settings from `~/.runway/sidecar.json`, CLI flags, or environment variables (`RUNWAY_URL`, `INGEST_API_KEY`).
*   **Deployment & Persistence:**
    *   **Linux Systemd:** One-line install script to register the headless binary as a `systemd` service.
    *   **Auto-Update:** Checks GitHub releases for new binaries. Downloads to a staging path, verifies checksum, replaces the old binary on next startup via a launcher script. Never hot-swaps a running binary.

---

## 📝 Documentation & Security

### Architecture Decision Records (ADRs)
**Path:** `docs/adr/` | **Effort:** 1 day

Formally document key architectural decisions:
*   Transition to stateful local-first design.
*   Environment vs. UI-based credential management.
*   Fernet encryption via `DB_ENCRYPTION_KEY` env var; plaintext fallback when unset.
*   `window_type` enum definitions and provider mappings.

### Troubleshooting & Setup Guide
**Path:** `docs/troubleshooting.md` | **Status:** ✅ Done (Apr 2026)

Centralized guide for expired tokens, 429 rate limits, and cross-platform cookie extraction. Includes Chrome App-Bound Encryption (ABE) diagnosis and platform-specific workarounds.

---

## 🔍 Established Architecture (Design Principles)

*These principles ensure Runway remains resilient and performant.*

### Collector Pattern & Multi-Tier Fallback
Each provider implements a multi-tier strategy to ensure the UI remains functional:
1.  **OAuth API**: Primary source for high-quality, real-time data.
2.  **Web API**: Secondary source using browser sessions/cookies.
3.  **Local Log Parsing**: Tertiary source for offline/unauthenticated metrics.
4.  **Graceful Error Cards**: Prevents UI crashes during total collection failure.

### Smart Differential Fetching
The `SmartCollector` wrapper manages the lifecycle of data fetching:
*   **TTL Caching**: Prevents API rate limiting (e.g., Gemini: 5m, Claude: 10m).
*   **Error Backoff**: Prevents hammering APIs during outages or 429 errors.
*   **Graceful Degradation**: Serves stale data with a `[Cached]` tag during temporary failures.

### Multi-Account & Tenant Isolation ✅ (Apr 2026)
*   **Account-Based Keying**: Backend keys tokens and snapshots by `(provider_id, account_id)`.
*   **Dynamic Collector Spawning**: `CollectorManager` iterates through all known active accounts, polling each independently.
*   **Automatic Identity Promotion**: Collectors discover account identities (Email/Org) from API responses and display them in the UI.

---

## 📌 Phase Summary

| Phase | Items | Theme |
|:---|:---|:---|
| **0** | 0A (CardBuilder), 0B (Schema Promotion) | Schema foundation — prerequisite for everything |
| **0.5** | S1–S6, T1–T7, P1–P3, A3 (audit fixes) | Hardening — security, stability, performance |
| **1** | 1A (SQLite), 1B (Settings UI), 1C (Background Polling) | Stateful core |
| **2** | 2A–2E (Health UI, Status API, OAuth Reset, Clock Skew, Browser Pref) | Quick wins — parallelizable |
| **3** | 3A (Modular Refactoring), 3B (Centralized Identity) | Architecture health |
| **4** | 4A (Dashboard Reorg), 4B (Fleet Mgmt), 4C (Background Refresh), 4D (Token Health) | Platform evolution |
| **5** | 5A (Charts), 5B (Export/Webhooks), 5C (Smart Polling), 5D (Native Desktop) | Polish & scale |

---

## 🚀 1.0 Release Prerequisites

These items must be completed before tagging v1.0, regardless of which phase they fall under:

- [x] All Phase 0 + 0.5 items complete
- [x] Phase 1A (SQLite) functional with retention policy
- [x] **API Versioning:** All routes under `/api/v1/` ✅ (Phase 3C)
- [x] **Single-Flight Collection (P1):** Request coalescing via `_collect_future` + instant registry ✅ (Phase 4C)
- [x] All 🔴 High severity audit items resolved (S1, S2, T1) ✅
- [ ] **Structured Logging:** Add JSON logging formatter option (`LOG_FORMAT=json`) for Docker/production deployments
- [ ] **Pydantic Settings (A1):** Refactor `Settings` to extend `pydantic_settings.BaseSettings` for type-safe env var handling
- [ ] **Data Retention Compaction:** Background job to downsample snapshots older than 30 days (see Phase 1A retention policy)

---

## 🔮 Post-1.0 Feature Ideas

| Feature | Description |
|:---|:---|
| **Server-Sent Events (SSE)** | Replace frontend polling with push-based updates. Natural fit with Phase 4C background refresh — eliminates the "poll → collect → respond" cycle. |
| **Per-Provider Refresh** | Allow the frontend to request a refresh for a single provider instead of triggering all collectors. |
| **Usage Alerts & Budget Caps** | User-defined thresholds (e.g., "alert me when Claude usage exceeds $50/month") stored in SQLite, evaluated by the background loop. |
| **Multi-User Mode** | Multiple Runway users sharing a single server (team deployment). Requires auth layer + per-user account isolation. |

*Last updated: 2026-04-13*
