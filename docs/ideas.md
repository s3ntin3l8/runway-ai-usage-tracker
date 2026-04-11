# Future Ideas & Improvements

This document tracks planned enhancements and architectural recommendations for Runway. Items are organized by category and status.

---

## 🏗️ Architecture & Core Logic

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

## 💻 Desktop Integration

### 6. Native Desktop Sidecar (Binary + Menubar App)
**Effort:** Large  
**Goal:** Distribute the sidecar as a true, zero-dependency desktop application that provides real-time visibility without opening a browser tab.

**Implementation Plan (PyInstaller + pystray):**
1. **GitHub Actions Build Pipeline:** Add `.github/workflows/build-sidecar.yml` to automatically compile `sidecar.py` into native executables using **PyInstaller** (`--windowed` for Mac, `--noconsole` for Windows). 
   - *Solves:* Python dependency hell for end-users.
2. **System Tray Integration:** Bake a lightweight UI directly into the sidecar binary using a library like `pystray`.
   - The tray icon acts as a quick-glance status, dynamically updating its color (Green/Yellow/Red) based on the lowest remaining quota.
   - Includes a native menu to "Restart", "Check for Updates", or "Open Dashboard".
3. **Application Branding:** Package the macOS build as a signed `.app` bundle with a custom `Info.plist`.
   - *Solves:* Replaces the generic OS "Python wants to access your keychain" prompts with a professional "Runway wants to access..." dialog.
4. **Auto-updating:** The compiled executable checks GitHub releases for a new binary, downloads it, and hot-swaps itself.

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
