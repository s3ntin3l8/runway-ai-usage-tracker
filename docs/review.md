# Runway Feature Review & Comprehensive Overview

This document provides a detailed overview of Runway's current feature set, architecture, and technical foundations to support the upcoming redesign.

## 🏛️ Core Architecture

Runway is a local-first monitoring platform built with a high-performance Python backend and a responsive, glassmorphism-inspired frontend.

- **Backend**: Python 3.12+, FastAPI, and SQLModel (on top of SQLAlchemy).
- **Storage**: SQLite-backed history with persistent state for fleet management and settings.
- **Frontend**: Vanilla HTML/JS with a custom CSS design system (glassmorphism) augmented by Tailwind CSS for layout utilities.
- **Async First**: The entire backend lifecycle — from API endpoints to individual provider collectors — is fully asynchronous to ensure high throughput and zero blocking on I/O.
- **Deployment Flexibility**:
  - **Standalone**: Single host running the main server (UI + collectors).
  - **Multi-Host**: Main server + remote Sidecars (data producers).
  - **Docker**: Containerized deployment with environment-based configuration.

---

## 📡 Collection Engine

The heart of Runway is its resilient collection logic, designed to extract data from providers even when APIs are unstable or uncooperative.

### 1. Multi-Tier Fallback Strategy
Collectors attempt to retrieve data through three progressive tiers:
1.  **Tier 1: OAuth/Official APIs**: The most reliable source for real-time, high-fidelity data.
2.  **Tier 2: Web Scraping/Session APIs**: Used when official APIs are unavailable, leveraging browser cookies (Safari/Firefox/Chrome).
3.  **Tier 3: Local Log Parsing**: Analyzes local log files (e.g., from IDE extensions or local servers) for zero-network metrics.

### 2. Smart Collection & Caching
- **`SmartCollector` Wrapper**: Every collector is wrapped in a smart caching layer that enforces TTLs (5–30 minutes) and handles exponential backoff for failed providers.
- **Adaptive Polling**: Features "dormancy tracking" that drops polling frequency for inactive accounts and snaps back to high resolution on usage detection.
- **Single-Flight Coalescing**: Concurrent requests to the `/limits` API join a single in-flight collection cycle, preventing redundant API calls to providers.

### 3. Dynamic Account Discovery
Runway automatically spawns collector instances for any accounts it discovers through:
- Hardcoded Environment Variables.
- Browser Safe Storage (macOS Keychain / Windows DPAPI).
- Sidecar Ingestion (remote sessions pushed to the main server).

---

## 🛸 Fleet Management

Runway is built to monitor usage across an entire fleet of workstations, not just the local host.

- **Sidecar Ingestion API**: A secured endpoint (`/api/v1/fleet/ingest`) that allows remote sidecars to push usage metrics signed with HMAC-SHA256.
- **Persistent Fleet Registry**: Automatically tracks all registered sidecars, including metadata like `last_seen`, `ip_address`, and `health_status`.
- **Sidecar UI**: A dedicated view for managing the fleet, with activity indicators and custom tagging.

---

## 🔐 Security & Access Control

Runway provides a multi-layered security model that balances ease of use with robust protection.

- **Local Trust**: Automated bypass for `127.0.0.1` access to ensure a seamless local development experience.
- **Login Portal**: Secure administrative portal triggered for remote access when `ADMIN_API_KEY` is set.
- **Headless Auth**: Native support for reverse proxies (Authelia, Nginx Auth, Cloudflare Access) via header trust (`Remote-User`).
- **Database Encryption**: Optional Fernet encryption for sensitive metadata in SQLite (keys, tokens) using `DB_ENCRYPTION_KEY`.

---

## 📊 Data & Analytics

Beyond real-time monitoring, Runway captures a rich history of usage patterns.

- **Usage Snapshots**: Every collection cycle persists high-resolution usage data to SQLite.
- **Data Compaction**: Automated background jobs that downsample old data (30+ days) to maintain performance without losing long-term trends.
- **History Visualizations**: Integrated Chart.js dashboards showing historical burn rates, costs, and token consumption.
- **Export Engine**: Support for CSV exports and Webhooks (Slack/Discord) for automated threshold alerts.

---

## 🎨 User Experience Highlights

The UI is designed to be informative at a glance while rewarding deep dives.

- **Glassmorphism Design**: A premium, "frosted glass" aesthetic with vibrant gradients and subtle micro-animations.
- **Contextual Filtering**: Instant filtering by Provider, Account, or Window Type.
- **Resilient UI**: Individual collector failures are isolated; they appear as "Error Cards" with retry actions, preventing a single failure from breaking the entire dashboard.
- **Token Health Panel**: A specialized view for monitoring the expiration of OAuth tokens and browser cookies with one-click refresh for supported providers.
- **Live Search**: Instant real-time filtering of quota cards.

---

## 🖼️ UI Component Inventory

For the purpose of redesign, the following core components are currently implemented:

### 1. The Global Header
- **Navigation Tabs**: Dashboard, History, Fleet, Settings.
- **Utility Actions**: Layout editing (Edit Mode), Theme Toggling (Light/Dark), Force Sync (Refresh).
- **Status Indicator**: Last updated timestamp.

### 2. Quota Card (The Core Unit)
- **Visuals**: Service name, icon (emoji), remaining value, and unit.
- **Badges**: Data source (OAuth, Web, Log, Sidecar), Account label, and Health status (Good, Warning, Critical).
- **Metrics**: Progress bar (remaining %), Reset timer, and "Pace" indicator (Stable, Moderate, Fast Burn).

### 3. Dashboard View
- **Health Bar**: A global summary of overall fleet/collector health.
- **Context Filter Bar**: Dimension selector and dynamic pills based on resident data.
- **Provider Sections**: High-level groupings of cards by platform.
- **Modal Details**: Deep-dive views triggered by clicking cards, showing raw metadata and history.

### 4. History Dashboard
- **Chart.js Integration**: Interactive line/bar charts with time-range switching (1h to 90d).
- **Metric Toggles**: Switch between "% used", "tokens", and "estimated cost".
- **Data Table**: Detailed tabular view of historical snapshots with CSV export.

### 5. Fleet Manager
- **Sidecar Cards**: Status dots (live recency), IP tracking, and ingestion stats.
- **Management Tools**: Custom naming, tag editing, and remote sidecar deletion.

### 6. Settings Panel
- **Provider Configuration**: Enable/disable toggles and custom poll intervals per provider.
- **Token Health Monitor**: Expiry tracking for all discovered credentials.
- **Webhook Configurator**: Rules-based alerts for Slack/Discord.
- **System Settings**: Global configuration overrides.

---

## 🚀 Technical State Summary

| Feature Category | Implementation Maturity | Storage / Tech |
| :--- | :--- | :--- |
| **Backend** | High (Modular, Mixin-based) | FastAPI, Pydantic v2 |
| **History** | Mature (Retention/Compaction) | SQLModel, SQLite |
| **Collectors** | Broad (14+ Providers) | httpx (Async) |
| **Fleet** | Mature (Registry + Monitoring) | SQLite, HMAC Auth |
| **Auth** | Multi-Layered (Key / Proxy) | JWT, secure cookies |
| **Frontend** | Modular (Tabbed, Responsive) | Vanilla JS, CSS Variables |

---

*Last Updated: 2026-04-19*
