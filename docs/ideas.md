# Future Ideas & Improvements

This document tracks planned enhancements and architectural recommendations for Runway. Items are organized by category and status.

---

## 🏗️ Core Platform Evolution (2026 Roadmap)

### 1. Stateful Usage & Configuration Hub
**Effort:** Large | **Status:** Architecture Approved (April 2026)
This is the foundational shift from a purely stateless monitor to a stateful local-first application using **SQLite** and **SQLModel**.

*   **Usage History:** A "Max Variant" schema capturing universal metrics (cost, tokens) and provider-specific JSON metadata for deep-dive trends.
*   **Settings UI:** A dedicated management page with a Top Navbar (Dashboard | History | Settings) to replace/augment `.env` configuration.
*   **Machine-Key Encryption:** Securing API keys in the local DB using `cryptography.fernet` to maintain Docker-friendly security.
*   **Passive Background Polling:** A 15-minute background loop ensuring data is captured even when the UI is closed, synchronized via TTL caches.
*   **Token Health & Proactive Refresh:** A "Health Dashboard" within settings showing exact expiry times for cookies/OAuth tokens. Includes a proactive background service to renew browser sessions before they expire and break workflows.
    > ⚠️ *Note (Apr 2026): Token refresh is significantly harder now that Chrome 127+ enforces App-Bound Encryption on macOS. Proactive cookie renewal is only feasible for OAuth tokens (can be refreshed via API) and Safari/Firefox cookies. Consider treating this as a separate, dedicated work item rather than bundling it into the stateful hub.*

### 2. Sidecar Fleet Management
**Effort:** Medium | **Status:** Planned
*   **Sidecar Registry:** A UI overview of all remote machines sending data to the central Runway instance.
*   **Centralized Remote Configuration:** Control exactly which APIs and log files each sidecar monitors directly from the main Runway Settings UI. Sidecars pull their specific configuration profile (e.g., enable/disable GitHub Copilot tracking on a specific machine) from the server upon connection.
*   **Environment/Project Tagging:** Allow users to assign "Tags" to sidecars (e.g., `Work_Laptop`, `Personal_Desktop`) to enable history filtering and accurate cost center reporting.
*   **Advanced Auth:** Support for rotating secrets or OIDC-based tokens for high-security multi-host deployments.

### 3. Intelligent Polling & Power Efficiency
**Effort:** Medium
*   **Dynamic Backoff ("Sleep Mode"):** Automatically detect user inactivity (no usage changes across 3 polls) and drop polling frequency to once every 2 hours to save battery and bandwidth.
    > ⚠️ *Clarification needed: "no usage changes" should be defined precisely — e.g., raw quota percentage unchanged AND no new API calls detected via log/local sources. Checking only raw numbers is insufficient since a stable quota with active usage would falsely trigger sleep mode.*
*   **Instant Wake:** Snaps back to high-frequency polling as soon as fresh usage is detected or the UI is opened.

### 4. Multi-Account & Tenant Isolation
**Effort:** Large | **Status:** Brainstorming
*   **The Problem:** Currently, the `TokenCache` and backend key tokens and snapshots solely by `provider_id` (e.g., `anthropic`). If multiple sidecars send different accounts' cookies, or one user rotates multiple accounts, they overwrite each other. This causes race conditions and broken "oscillating" history charts as the poll flips between different account limits.
*   **Account-Based Keying:** Refactor the backend to key tokens and database snapshots by a unique `account_id` or `profile_name` (e.g., `(provider_id, account_hash)`).
*   **Smart Collector Iteration:** Update collectors to iterate through all known active accounts for a provider, polling each independently and generating separate `LimitCard` entries (e.g., "Claude Pro (Work)", "Claude Free (Personal)").
*   **UI Aggregation:** Add the ability for users to choose whether to aggregate multiple accounts into a single "Total Quota" or split them out individually.

---

## 📊 Dashboard & UI Enhancements

### 5. Context-Aware Dashboard Reorganization (Evolution)
**Effort:** Medium | **Status:** Architecture Approved (April 2026)
As the number of providers, sidecars, and multi-account configurations grows, the flat grid will be reorganized into a more structured hierarchy.
- **Grouping by Provider (Sectioned Grid):** Transition from a single flat grid to horizontal sections grouped by `provider_id` (e.g., an "Anthropic" section followed by an "OpenAI" section), each with its own header and logo.
- **Context Filters:** Add segmented control pills at the top of the dashboard (e.g., `[All]`, `[Work]`, `[Personal Laptop]`, `[Alice's Account]`) to instantly filter visible cards based on sidecar tags or account profiles.
- **Visual Badging & Avatars:** Add elegant corner badges or tiny avatars to cards to identify their source (e.g., a laptop icon for a specific sidecar, or an initial for a user account) without breaking the glassmorphism aesthetic.

### 6. Chart.js Visualizations
**Effort:** Medium
Replace static cards with interactive time-series charts showing financial burn rates, token volume trends, and comparative provider usage.

### 7. Metrics Export & Webhooks
*   **CSV Expense Reporting:** Add a "Download CSV" button to the History page to generate formatted expense reports for tax write-offs or employer reimbursement.
*   **Prometheus:** Add `/api/limits?format=prometheus` for external monitoring integrations.
*   **Webhooks:** Send Discord/Slack alerts when quotas cross critical thresholds (e.g., >90% used).

---

## 💻 Desktop Integration

### 8. Native Desktop Sidecar (Binary + Menubar App)
**Effort:** Large  
**Goal:** Distribute the sidecar as a true, zero-dependency desktop application (PyInstaller) that provides real-time visibility and configuration across both GUI and headless environments.

*   **OS-Native Desktop Notifications:** Leverages macOS/Windows notification centers to warn users about critical quota limits or expired browser sessions directly on their desktop.
*   **Hybrid Operation (GUI & Headless):**
    *   **Desktop Mode:** A system tray icon (Windows) or Menubar app (macOS) with a right-click menu showing connection status, last sync, and a "Settings..." option.
    *   **Headless Mode:** Automatically detects environments without a display (Linux servers, Docker, VPS) or uses a `--headless` flag to run as a pure background daemon.
*   **Lightweight Configuration:**
    *   **GUI Settings:** Uses Python's built-in `tkinter` for a zero-dependency, native dialog box to configure the Central Runway URL and Ingest API Key.
    *   **Unified Config:** Reads settings from `~/.runway/sidecar.json`, CLI flags, or environment variables (`RUNWAY_URL`, `INGEST_API_KEY`).
*   **Deployment & Persistence:**
    *   **Linux Systemd:** Provides a one-line install script to register the headless binary as a `systemd` service for permanent background execution on code servers.
    *   **Auto-Update:** The compiled executable checks GitHub releases for a new binaries and hot-swaps itself.

---

## 🛠️ Developer & Operational Experience

### 12. Collector Health Status Endpoint
**Effort:** Small | **Status:** New

`SmartCollector` already implements a `get_stats()` method returning cache age, consecutive error count, last error message, and TTL for every collector. However, this data is never exposed externally — it's only visible in debug logs.

*   **`GET /api/status`:** A lightweight endpoint that returns `get_stats()` output from all active `SmartCollector` instances, giving operators a live view of which collectors are healthy, degraded (serving stale cache), or in error backoff — without needing to inspect logs.
*   **Zero new logic required** — just wire `CollectorManager.smart_collectors` to the new route.

### 13. OAuth Terminal Failure Reset
**Effort:** Small | **Status:** New

In `OAuthBaseCollector`, the `_terminal_failure = True` flag (set on `invalid_grant` responses) permanently disables OAuth refresh for that collector's lifetime. The only recovery path is a **server restart** — there is no UI or API mechanism to reset it.

*   **`POST /api/reset/{provider}`:** A simple admin endpoint that clears `_terminal_failure` and resets the error count on the relevant `SmartCollector`, allowing a re-auth attempt after a user has re-logged in without bouncing the server.
*   **UI button:** A "Retry Auth" button in the error card tooltip/detail for `auth_failed` error type cards.

### 14. Sidecar Clock Skew Detection
**Effort:** Small | **Status:** New

The ingest endpoint in `ingest.py` silently rejects payloads where `|server_time - sidecar_timestamp| > 300s` with a generic 401. If a remote machine has drifted NTP, the sidecar will fail silently with no actionable feedback in the sidecar logs.

*   **Structured error response:** Return `{"detail": "timestamp_expired", "skew_seconds": 312}` so the sidecar can log a specific, actionable clock sync warning.
*   **Sidecar-side warning:** Log `⚠️ Clock skew detected — check NTP sync` when a 401 with `timestamp_expired` is received, rather than a generic auth error.

### 15. Browser Preference Ordering
**Effort:** Small | **Status:** New

`get_all_browser_cookies_paths()` returns browsers in a hardcoded order: Safari first on macOS, then Chrome/Chromium/Edge, then Firefox. The first browser with a matching cookie wins. This means a stale Safari session will silently shadow an active Firefox or Chrome session.

*   **`BROWSER_PREFERENCE` env var:** A comma-separated ordered list (e.g., `BROWSER_PREFERENCE=firefox,safari,chrome`) to override the default discovery order per deployment.
*   **Especially useful on macOS** where Safari is default-first but developers may primarily use Firefox or have explicitly logged into a provider there.

---

## 📝 Documentation & Security

### 9. Architecture Decision Records (ADRs)
**File:** `docs/adr/` | **Effort:** 1 day
Formally document the transition to stateful local-first design and environment vs. UI-based credentials.

### 10. Troubleshooting & Setup Guide
**File:** `docs/troubleshooting.md` | **Status:** ✅ Done (Apr 2026)
Centralized guide for expired tokens, 429 rate limits, and cross-platform cookie extraction. Now includes Chrome App-Bound Encryption (ABE) diagnosis and platform-specific workarounds.

### 11. In-UI Credential & Cookie Health Indicators
**Effort:** Small | **Status:** New
Surface the credential source and health state directly on each quota card in the dashboard, without needing to open a separate settings page. This is especially valuable given Chrome ABE making silent failures more common.

*   **Data Source Badge:** A small pill/icon on each card showing where data came from (e.g., `OAuth`, `Safari Cookie`, `ENV`, `Sidecar`, `Cached`) using existing `data_source` field already present in `LimitCard`.
*   **Credential Status Indicator:** A `⚠️` badge when the credential used is known-expired or approaching expiry (e.g., OAuth token within 1 hour of expiry).
*   **Browser Fallback Notice:** When Chrome cookie extraction fails due to ABE, surface a subtle inline hint on affected cards (e.g., *"Login detected in Chrome but could not decrypt — try Safari"*) instead of silently showing an error card.
*   **Implementation:** No new backend needed — the `data_source`, `error_type`, and `updated_at` fields in `LimitCard` already carry the necessary metadata. This is purely a frontend rendering enhancement.

---

## 🔍 Established Architecture (Design Principles)

*These principles ensure Runway remains resilient and performant.*

### Collector Pattern & Multi-Tier Fallback
Each provider implements a multi-tier strategy to ensure the UI remains functional:
1. **OAuth API**: Primary source for high-quality, real-time data.
2. **Web API**: Secondary source using browser sessions/cookies.
3. **Local Log Parsing**: Tertiary source for offline/unauthenticated metrics.
4. **Graceful Error Cards**: Prevents UI crashes during total collection failure.

### Smart Differential Fetching
The `SmartCollector` wrapper manages the lifecycle of data fetching:
- **TTL Caching**: Prevents API rate limiting (e.g., Gemini: 5m, Claude: 10m).
- **Error Backoff**: Prevents hammering APIs during outages or 429 errors.
- **Graceful Degradation**: Serves stale data with a `[Cached]` tag during temporary failures.

---

---

## 📌 Recommended Priority Sequence (Apr 2026)

Based on risk, dependency order, and user impact:

| Priority | Item | Reason |
|:---|:---|:---|
| **1** | **#4 — Multi-Account Keying** | Latent data corruption — gets worse as fleet grows. Fix the foundation before building on it. |
| **2** | **#1 — Stateful Usage Hub** | Required foundation for history, settings UI, alerting, and multi-account support. |
| **3** | **#11 — Credential Health Indicators** | Low-effort, high-value UX improvement. Uses existing `LimitCard` metadata — no backend work. |
| **3** | **#12 — Collector Status Endpoint** | Zero new logic — `get_stats()` already exists; just needs a route. Invaluable for ops. |
| **3** | **#13 — OAuth Terminal Failure Reset** | Tiny fix, high pain relief — currently requires a server restart after re-login. |
| **3** | **#14 — Sidecar Clock Skew Detection** | Small change to `ingest.py`; prevents silent failures in multi-host deployments. |
| **4** | **#15 — Browser Preference Ordering** | Small env var addition; removes the stale-Safari-wins problem for Firefox-first users. |
| **5** | **#5 — Dashboard Reorganization** | Natural evolution once stateful hub and multi-account are in place. |
| **6** | **#8 — Native Desktop Sidecar** | Highest adoption leverage; build on stable foundation from items 1–5. |
| 7 | #2 — Sidecar Fleet Management | Depends on stateful hub (#1) for remote config storage. |
| 7 | #7 — Export & Webhooks | Depends on history (#1) for meaningful data export. |
| 8 | #3 — Dynamic Backoff | Polish; only meaningful once background polling (#1) exists. |
| 8 | #6 — Chart.js Visualizations | Polish; depends on history data from #1. |

*Last updated: 2026-04-12*
