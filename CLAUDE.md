# CLAUDE.md - Runway (AI Usage Tracker)

Runway is a local-first monitoring tool for AI provider quotas with SQLite-backed history.

## Architecture
- **Three Modes**: Standalone, Multi-Host (Sidecar), Docker (Sidecar required).
- **Docker Rule**: No native desktop UI/keychains — use ENV vars or sidecar-fed data.
- **Cookie Collectors**: Claude, ChatGPT, Ollama, Kimi Coding, OpenCode need browser cookies; in Docker provide via ENV.

## Commands
A `Makefile` wraps all common tasks — run `make help` for the full list. Key targets:
- **Setup**: `make install` (also wires up the pre-push hook that runs lint + tests)
- **Dev server**: `make dev` (port 8765, hot reload)
- **Tests**: `make test` (ignores `test_browser_cookies.py` — macOS-only crypto, fails on Linux/WSL)
- **Single test**: `pytest tests/path/to/test_file.py`
- **Lint**: `make lint` (ruff + mypy + pip-audit)
- **Sidecar**: `make sidecar`

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

### Fallback Pattern (Legacy)
Traditional multi-tier fallback: primary strategy runs first, if it fails, try next, etc.
- Example: `api` → `web` → `local`

### Enrichment Pattern (New)
Primary strategy runs first. If successful, enrichment strategies run *in addition* and merge their data into the primary results.
- Use case: Combining API quota limits with local token usage
- Example: API returns quota/limits, local session logs add token usage breakdown
- Implementation: Strategies marked with `{"enrich": True}` in STRATEGIES dict

#### Defining Enrichment Strategies
```python
STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
    "api": ("API", "_collect_via_api"),                              # Primary - provides main card data
    "local": ("Local", "_collect_via_logs", {"enrich": True}),       # Enrichment - adds token details
}
```

The enrichment data is merged into the primary card's `detail` string. Override `_enrich_results()` in subclasses for custom merging logic.
