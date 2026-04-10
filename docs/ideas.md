# Future Ideas & Improvements

This document tracks planned enhancements for Runway. Items are organized by category and priority.

---

## Medium Priority

### 4. Move Away from Hardcoded Limits
**Files:** `app/services/collectors/*.py`  
**Effort:** 1-2 days

Query local IDE config files for plan information instead of hardcoded limits (e.g., 2M tokens for Claude).

## Low Priority

### 8. Formalize Strategy Pattern
**File:** `app/services/collectors/base.py`  
**Effort:** 2-3 hours

Implement abstract methods (`_primary_strategy()`, `_fallback_strategy()`, `_error_handler()`) to enforce 3-tier fallback consistency.

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
**Reference:** [Robust Strategy Implementation](https://github.com/steipete/CodexBar/blob/main/docs/antigravity.md)

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

*Last updated: 2026-04-10*
