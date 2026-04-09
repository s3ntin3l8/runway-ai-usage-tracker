# CLAUDE.md - Runway (AI Usage Tracker)

Runway is a local-first, stateless monitoring tool for AI provider quotas.

## Architecture
- **Three Modes**: Standalone, Multi-Host (Sidecar), and Docker (Sidecar required).
- **The Docker Rule**: DO NOT use native desktop UI/keychains in code; use ENV or sidecar-fed data.

## Essential Commands
- **Setup**: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- **Run (Dev)**: `uvicorn app.main:app --reload --port 8765`
- **Run (Production)**: `python3 -m app.main`
- **Sidecar**: `python3 scripts/sidecar.py`
- **Test (All)**: `pytest`
- **Test (Single)**: `pytest tests/unit/test_file.py`
- **Manual Ingest Test**: `python3 scripts/test_ingest.py`

## Code Style & Patterns
- **Backend**: Python 3.9+, FastAPI, Pydantic v2, `httpx` (async).
- **Frontend**: Vanilla CSS (glassmorphism) + Tailwind CSS.
- **Async Required**: Everything from endpoints to collectors MUST be `async`.
- **Typing**: Use explicit type hints for all API responses and internal models.
- **Error Handling**: Graceful degradation; return "Error Card" states instead of crashing.
- **Surgical Precision**: Preserve existing comments and structure during edits.

## Workflow
- **Statelessness**: Avoid persistent databases; prefer local files or ENV variables.
- **Test Preference**: Prefer running single tests over the full suite for performance (e.g. `pytest tests/unit/test_claude.py`).
- **Sidecar Focus**: Sidecars only extract/forward raw data; the main server does heavy lifting (API calls).