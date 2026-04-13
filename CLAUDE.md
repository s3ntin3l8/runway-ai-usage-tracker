# CLAUDE.md - Runway (AI Usage Tracker)

Runway is a local-first monitoring tool for AI provider quotas with SQLite-backed history.

## Architecture
- **Three Modes**: Standalone, Multi-Host (Sidecar), Docker (Sidecar required).
- **Docker Rule**: No native desktop UI/keychains — use ENV vars or sidecar-fed data.
- **Cookie Collectors**: Claude, ChatGPT, Ollama need browser cookies; in Docker provide via ENV.

## Commands
A `Makefile` wraps all common tasks — run `make help` for the full list. Key targets:
- **Setup**: `make install`
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

## Code Style & Patterns
- **Backend**: Python 3.12+, FastAPI, Pydantic v2, `httpx` (async).
- **Frontend**: Vanilla CSS (glassmorphism) + Tailwind CSS.
- **Async Required**: Everything from endpoints to collectors MUST be `async`.
- **Typing**: Explicit type hints on all API responses and internal models.
- **Error Handling**: Graceful degradation — return "Error Card" states instead of crashing.
- **Surgical Precision**: Preserve existing comments and structure during edits.
- **Sidecar Focus**: Sidecars only extract/forward raw data; main server does the heavy lifting.
