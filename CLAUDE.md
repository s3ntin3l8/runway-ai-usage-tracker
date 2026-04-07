# CLAUDE.md - Runway (AI Usage Tracker)

## 🎯 Project Identity & Context
**Runway** is a local-first, stateless monitoring tool for tracking AI provider quotas and balances (Claude, Gemini, ChatGPT, GitHub Copilot, etc.). It is designed to be modular and resilient, often running in containerized or headless environments.

## 🏗️ Architecture & Data Fetching

Runway operates in **three deployment modes**, with data collection strategies adapting to each:

### Mode 1: Standalone (Bare Metal)
App runs directly on the coding workstation (Linux/Mac/Windows).
- **Local Access**: Directly reads local files (`~/.claude/`, `opencode.db`, etc.)
- **Cookie Extraction**: Extracts Chrome cookies locally
- **Web APIs**: Calls provider APIs using extracted tokens
- **Sidecar**: NOT needed (redundant)

### Mode 2: Multi-Host (Main PC + Laptop)
One computer runs the main app, others send data via sidecar.
- **Main Host**: Scrapes own local data + receives sidecar data
- **Secondary Hosts**: Run sidecar script only (extracts cookies/files, sends to main)
- **Aggregation**: Main app combines all sources

### Mode 3: Server/Docker
App runs containerized, no local filesystem access.
- **Local Files**: NONE (container has no host access)
- **Web APIs**: Uses tokens/cookies sent by sidecars
- **Sidecars**: REQUIRED on ALL workstations (provide raw data only)
- **Golden Rule**: Heavy lifting (API calls, aggregation) happens server-side

### Core Principles
1. **Server Does Heavy Lifting**: API calls, aggregation, dashboard logic run ONLY on main app
2. **Sidecar is Thin**: Only extracts and forwards raw data (cookies, tokens, DB files)
3. **No Duplication**: If main app can access directly (standalone), don't use sidecar
4. **Docker = Sidecar Only**: In containers, ALL data comes from sidecars + web APIs

### Implementation Details
- **Modular Services**: Each provider has a dedicated collector in `app/services/collectors/`.
- **API First & Local Fallback**: Prefers direct HTTP requests; falls back to local log parsing (e.g., `~/.claude/activity.log`) when necessary.
- **Sidecar Ingestion**: Supports external metrics via `POST /api/ingest` (Ingestion API).
- **Stateless**: No centralized database. Uses Pydantic for validation and in-memory aggregation.

## 🚫 Absolute Constraints (The Docker Rule)
Avoid writing code that relies on native desktop UI features if the app is intended for a headless Docker environment:
- **DO NOT** attempt to scrape desktop-only keychains if not supported by the environment.
- **Paths**: Use environment variables or relative paths configurable via `.env`.

## 💻 Tech Stack & Coding Standards
- **Runtime**: Python 3.9+ (FastAPI).
- **Concurrency**: `httpx` (async) for all network calls.
- **Validation**: Pydantic v2 for models (`app/models/`).
- **Styling**: Vanilla CSS (Modern glassmorphism) + Tailwind CSS in the frontend.

### Commands
- **Install**: `pip install -r requirements.txt`
- **Run (Dev)**: `uvicorn app.main:app --reload --port 8765`
- **Run (Production)**: `python3 -m app.main`
- **Test (Ingest API)**: `python3 scripts/test_ingest.py`
- **Manual Test (CURL)**: `curl -X POST http://localhost:8765/api/ingest -H "Content-Type: application/json" -d '{"provider": "claude", "metrics": {...}}'`

### Coding Patterns
- **Error Handling**: Graceful degradation. If a provider API fails, catch the error and return an "Error Card" status instead of crashing the dashboard.
- **Async**: Everything from endpoint to collector should be `async`.
- **Typing**: Use explicit type hints and Pydantic models for all API responses and internal data structures.
- **Surgical Precision**: Only modify what is strictly necessary. Preserve existing comments and structure.
- **Reasoning Phase**: For complex logic changes, briefly explain the architectural approach before outputting code.

## 🔐 Token Transmission Architecture

### Overview
Runway uses a **hybrid token architecture** where the server does all API calls, but can use tokens extracted by sidecars from other machines:

**Server Responsibilities:**
- Makes ALL API calls (OAuth, Web API, direct API)
- Aggregates data from multiple sources
- Serves the dashboard

**Sidecar Responsibilities:**
- Extracts tokens/cookies from local files (keychain, browser cookies, config files)
- Reads local-only data files (SQLite, JSON logs)
- Sends tokens to server via `/api/ingest`
- Does NOT make API calls

### Token Flow
1. Sidecar extracts tokens from local sources (every 30 minutes via cron)
2. Sidecar sends tokens to server via `/api/ingest` endpoint
3. Server stores tokens in **in-memory cache** (30-minute TTL)
4. Server uses tokens to make API calls on behalf of the sidecar
5. Results are displayed with `data_source` indicating the API type used

### Token Storage
- **Memory-only**: Tokens stored in `app.services.token_cache` (30-min TTL)
- **Stateless**: Lost on server restart, refreshed by sidecar on next run
- **Security**: Tokens never persisted to disk

### Data Source Values
| Value | Meaning | Set By |
|-------|---------|--------|
| `oauth` | OAuth API call (e.g., api.anthropic.com) | Server |
| `web_api` | Cookie-based web scraping | Server |
| `api` | Direct API call with key | Server |
| `local` | Local file reading (DB, logs) | Server or Sidecar |
| `cache` | Cached/stored data | Server or Sidecar |

### Token Priority (Per Provider)
1. Environment variables (server local)
2. Token cache from sidecar (if available)
3. Server local files/cookies
4. Sidecar local data (via external_metrics)
5. Fallback to logs

### Implementation
- **Sidecar script**: `scripts/sidecar.py` - Extracts only, no API calls
- **Token cache**: `app/services/token_cache.py` - In-memory storage
- **Ingest endpoint**: `app/api/endpoints/ingest.py` - Receives and parses tokens
- **Collectors**: Check token cache before making API calls

## 🤖 Behavior Guidelines
1. **Be Resilient**: Always consider what happens if an external API is down or a file is missing.
2. **Prioritize UI**: Frontend changes should be premium, high-performance, and maintain the glassmorphism aesthetic.
3. **Keep it Stateless**: Avoid adding persistent databases unless strictly required for a new feature (prefer local file parsing/ENV).
4. **Local Testing**: Create all test scripts (e.g., in `scripts/`) within the project folder to ensure they are portable, reusable, and version-controlled.