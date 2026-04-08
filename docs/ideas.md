# Future Ideas & Improvements

This document tracks planned enhancements for Runway. Items are organized by category and priority.

---

## High Priority

### 1. GitHub OAuth Device Flow
**File:** `app/services/collectors/github.py` + frontend  
**Effort:** 6-8 hours

Replace manual `GITHUB_TOKEN` entry with [GitHub Device Flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow).
- Display user code in frontend
- Poll for access token in background
- Useful for headless/Docker environments

### 2. ChatGPT Web Dashboard Scraping
**File:** `app/services/collectors/chatgpt.py`  
**Effort:** 1-2 days

Scrape `https://chatgpt.com/codex/settings/usage` for rate limits, credits, detailed charts.
- Support manual Cookie header input
- Support automatic cookie extraction (Safari/Chrome/Firefox on macOS)

---

## Medium Priority

### 3. Dashboard Auto-Refresh UI Toggle ✅
**File:** `frontend/index.html` + `frontend/js/app.js`  
**Effort:** 2-3 hours  
**Status:** Implemented

Add auto-refresh toggle with intervals (30s, 60s, 5m). Store preference in `localStorage`.

**Features:**
- Cycle through OFF → 30s → 60s → 5m → OFF with single button click
- Visual pulsing dot indicator when auto-refresh is active
- Safe to use: server caches API responses for 5-10 minutes
- Timer cleanup on page unload

### 3b. Bright Mode Theme Toggle ✅
**File:** `frontend/css/input.css` + `frontend/js/app.js`  
**Effort:** 1-2 hours  
**Status:** Implemented

Add light/bright mode theme toggle for better visibility in well-lit environments.

**Features:**
- ☀️/🌙 toggle button with smooth transitions
- Complete color palette swap (dark → bright)
- All UI elements themed: cards, modals, buttons, scrollbars
- Preference persisted in `localStorage`
**File:** `frontend/index.html` + `frontend/js/app.js`  
**Effort:** 2-3 hours  
**Status:** Implemented

Add auto-refresh toggle with intervals (30s, 60s, 5m). Store preference in `localStorage`.

**Features:**
- Cycle through OFF → 30s → 60s → 5m → OFF with single button click
- Visual pulsing dot indicator when auto-refresh is active
- Safe to use: server caches API responses for 5-10 minutes
- Timer cleanup on page unload

### 4. Move Away from Hardcoded Limits
**Files:** `app/services/collectors/*.py`  
**Effort:** 1-2 days

Query local IDE config files for plan information instead of hardcoded limits (e.g., 2M tokens for Claude).

### 5. Multi-Browser Cookie Support
**Files:** `app/core/chrome_cookies.py`  
**Effort:** 4-6 hours

Add support for:
- **Firefox** (`cookies.sqlite`)
- **Safari** (`Cookies.binarycookies`, macOS only)
- **Edge** (Chromium-based)

### 6. Sidecar Daemon Mode
**File:** `scripts/sidecar.py`  
**Effort:** 2-3 hours

Support `--daemon` flag for persistent process with configurable sleep interval (more real-time than 30m crontab).

### 7. Sidecar Offline Queuing
**File:** `scripts/sidecar.py`  
**Effort:** 4-6 hours

If ingestion API unreachable, cache metrics locally and retry on next connection.

---

## Low Priority

### 8. Formalize Strategy Pattern
**File:** `app/services/collectors/base.py`  
**Effort:** 2-3 hours

Implement abstract methods (`_primary_strategy()`, `_fallback_strategy()`, `_error_handler()`) to enforce 3-tier fallback consistency.

### 9. Docker Multi-Stage Build
**File:** `Dockerfile`  
**Effort:** 1-2 hours

Use builder stage to reduce final image size:
- Install build deps in builder stage
- Copy only `/opt/venv` to final `python:3.12-slim-bookworm` stage

### 10. Binary Sidecar Distribution
**File:** `sidecar/` (build scripts)  
**Effort:** 1-2 days

Distribute sidecar as single binary (PyInstaller) to avoid Python dependency issues.

---

## Documentation

### 11. Architecture Decision Records (ADRs)
**File:** `docs/adr/`  
**Effort:** 1 day

Document key decisions:
- Local-first over centralized API
- Stateless design (no database)
- Environment-based credentials

### 12. Troubleshooting Guide
**File:** `docs/TROUBLESHOOTING.md`  
**Effort:** 2-3 hours

Centralized guide for: expired tokens, 429 rate limits, cookie extraction failures.

---

## Future Research

### 13. Historical Tracking & Burndown Charts
Track usage over time in local SQLite DB (`~/.runway/history.db`) for trend analysis. (Note: violates stateless principle).

### 14. Metrics Export Formats
Add `/api/limits?format=prometheus` or `format=csv` for external monitoring integration.

### 15. Webhook Notifications
Send Discord/Slack alerts when quotas cross thresholds (e.g., >90% used).

---

## Collector-Specific Ideas

### Claude

#### CLI PTY Parsing (5th Tier Fallback)
Spawn `claude` CLI in PTY and parse `/usage` output. Would slot before error cards:
```
OAuth API → Web API → Local Logs → CLI PTY → Error Cards
```

| Aspect | CLI PTY |
|--------|---------|
| **Requires** | CLI binary |
| **Data Quality** | Complete |
| **Speed** | Slow (process spawn) |
| **Reliability** | Low (fragile parsing) |

#### Alternative Endpoint: v1/rate_limits
**Endpoint:** `https://api.anthropic.com/v1/rate_limits`

Simpler data structure (single window vs per-window). Could add between OAuth and Web API.

#### Firefox/Safari/Edge Cookie Support
Extend `chrome_cookies.py` to support other browsers.

#### Windows Credential Store
Add Windows Credential Manager support (currently macOS Keychain only).

### Gemini

#### CLI `/stats` Parsing
Parse `gemini /stats` CLI output for quota percentages. Would slot between OAuth API and session logs.

### OpenCode

#### Direct API Key Authentication
**Status:** Deprecated. OpenCode endpoints (`api.opencode.ai/v1/user/usage`) now return 404. Continue using Chrome cookie authentication.

### Kimi API

#### Usage History API
Query usage history for daily/monthly spend tracking, model-specific breakdown.

### Antigravity

#### File Watching
Use `watchdog` or `inotify` to watch for quota file changes instead of polling.

#### LSP Protocol Approach
**Reference:** [CodexBar Implementation](https://github.com/steipete/CodexBar/blob/main/docs/antigravity.md)

Use active LSP protocol instead of passive file reading:
1. Detect `language_server_macos` process
2. Probe listening ports with HTTPS POST
3. Call `GetUserStatus` endpoint with CSRF token

| Aspect | File-Based | LSP Protocol |
|--------|------------|--------------|
| **Requires** | File permission | Process + port scan |
| **Reliability** | IDE-dependent | Real-time |
| **Complexity** | Low | High |

**Priority:** Low-Medium

---

*Last updated: 2026-04-08*
