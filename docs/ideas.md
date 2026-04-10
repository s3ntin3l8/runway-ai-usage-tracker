# Future Ideas & Improvements

This document tracks planned enhancements and architectural recommendations for Runway. Items are organized by category and status.

---

## 🏗️ Architecture & Core Logic

### 1. Logic Unification (Shared Registry)
**Status:** Planned  
**Source:** Code Review
Extract common collector logic (endpoints, headers, parsing rules) into a shared `registry.json`. 
- **Goal:** Reduce logic drift between `app/` and `scripts/sidecar.py`.
- **Implementation:** Use a "Sidecar Generator" script (`scripts/generate_sidecar.py`) to keep the sidecar zero-dependency while maintaining a single source of truth.

### 3. Binary Sidecar Distribution
**File:** `sidecar/` (build scripts)  
**Effort:** 1-2 days
Distribute the sidecar as a single binary (using PyInstaller or similar) to avoid Python dependency issues on host machines.

---

## 📊 Dashboard & Visualization

### 4. Trend Visualization & History
**Effort:** Medium  
**Source:** Code Review
- **SVG Sparklines:** Add subtle usage curves to each card.
- **Local History:** Store usage data for the last 24 hours in `localStorage` or a lightweight `history.db` (SQLite) at `~/.runway/history.db`.
- **Detail Drill-down:** Enhance the existing modal to show raw logs or detailed usage breakdown.

### 5. Metrics Export & Webhooks
- **Prometheus/CSV:** Add `/api/limits?format=prometheus` for external monitoring.
- **Webhooks:** Send Discord/Slack alerts when quotas cross thresholds (e.g., >90% used).

---

## 🔌 Collector-Specific Enhancements

### Claude
- **Query Local Configs:** Move away from defaults and query local IDE config files for specific plan information instead of hardcoded limits.

### Gemini
- **CLI `/stats` Parsing:** Parse `gemini /stats` CLI output for quota percentages. Would slot between OAuth API and session logs.

### Kimi API
- **Usage History API:** Query usage history for daily/monthly spend tracking and model-specific breakdowns.

### Antigravity
- **File Watching:** Use `watchdog` or `inotify` to watch for quota file changes instead of polling.
- **LSP Protocol Approach:** Use the active LSP protocol instead of passive file reading.
  1. Detect `language_server_macos` process.
  2. Probe listening ports with HTTPS POST.
  3. Call `GetUserStatus` endpoint with CSRF token.

---

## 💻 Desktop Integration

### 6. Menubar / System Tray App
**Effort:** Medium
- **Goal:** Real-time visibility without having a browser tab open.
- **Implementation:** A lightweight Python script (using `pystray` or `rumps`) that polls the local Runway API and displays critical quotas in the system tray.
- **Dynamic Icons:** Update the tray icon color (Green/Yellow/Red) based on the lowest remaining quota.

---

## 📝 Documentation & Security

### 6. Architecture Decision Records (ADRs)
**File:** `docs/adr/`  
**Effort:** 1 day
Document key decisions:
- Local-first over centralized API
- Stateless design (no database)
- Environment-based credentials

### 7. Troubleshooting Guide
**File:** `docs/TROUBLESHOOTING.md`  
**Effort:** 2-3 hours
Centralized guide for: expired tokens, 429 rate limits, cookie extraction failures.

### 8. Advanced Sidecar Authentication
**Source:** Code Review
While HMAC signing is implemented, consider adding support for rotating secrets or OIDC-based tokens for high-security Multi-Host deployments.

---

## 🔍 Architecture Insights (from 2026 Code Review)

*The following insights were captured during a comprehensive review of the Runway architecture.*

### Stateless & Local-First Philosophy
The project adheres strictly to a stateless, "no-database" design. It favors direct Web API calls, log parsing from local tools, and in-memory aggregation. This architecture is perfectly suited for zero setup cost and high privacy.

### Collector Pattern & Resilience
Each provider implements a multi-tier fallback strategy:
1. **OAuth API**: Primary source for high-quality, real-time data.
2. **Web API**: Secondary source using browser sessions/cookies.
3. **Local Log Parsing**: Tertiary source for offline/unauthenticated metrics.
4. **Graceful Error Cards**: Ensures UI remains functional even during total collection failure.

### Smart Differential Fetching
The `SmartCollector` wrapper handles:
- **TTL Caching**: Prevents rate limiting (e.g., Gemini: 5m, Claude: 10m).
- **Error Backoff**: Prevents hammering APIs during outages.
- **Graceful Degradation**: Serves stale data with a `[Cached]` tag during failures.

---

*Last updated: 2026-04-10*
