# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-04-13

### Added
- **Chart.js Visualizations (Phase 5A)**: Interactive token volume charts on the History tab with stacked bar and line toggles.
- **CSV Export (Phase 5B)**: Ability to download usage history as CSV via the UI and `/api/v1/usage/history?format=csv`.
- **Webhook Alerts (Phase 5B)**: Full CRUD API and background monitoring for usage breaches with Discord and Slack support.
- **Smart Polling (Phase 5C)**: Implemented "sleep mode" — 2h interval after 45min of no quota change to optimize resource usage.
- **Data Retention & Compaction (Phase 4.5)**: Background compaction service (60d hourly, 180d daily) to manage database growth.
- **Pydantic Settings (Phase 4.5)**: Migrated configuration to `pydantic-settings` for more robust environment variable handling.
- **Structured JSON Logging (Phase 4.5)**: Added `JsonFormatter` for `LOG_FORMAT=json` support, ideal for containerized environments.
- **Fleet Registry & Token Health (Phase 4)**: Improved fleet management, status tracking for multi-host deployments, and real-time health checks for cached credentials.
- **Instant-Cache Serving (Phase 4)**: Optimized poller and collector management for faster dashboard loading.
- **Modernized CI/CD Infrastructure**:
    - Integrated **Ruff** for linting and formatting, **Mypy** for type analysis.
    - Added **pip-audit** for dependency vulnerability scanning (no API key required).
    - Added **Hadolint** for Dockerfile linting and **Frontend Check** for CSS builds.
    - Added **Dependabot** for automated weekly updates (actions, pip, npm).
    - Added **Codecov** integration with coverage thresholds (63% project, 70% patch).
    - Added concurrency groups (cancel stale PR runs) and job timeouts.
    - Added **Makefile** with `install`, `dev`, `test`, `lint`, `format`, `css`, `sidecar`, `clean` targets.
    - Implemented `pyproject.toml` for centralized tool configuration.
    - Reorganized `.gitignore` and `.dockerignore` for better repository hygiene.
- **Test Coverage**: Added 82 new tests (317 total); coverage improved from 65% → 69%.
    - New: `test_token_refresher.py`, `test_builder.py`, `test_utils.py`.
    - Fixed 4 stale patch targets broken by the `oauth_base` refactor.

### Changed
- Refined Dashboard UI with glassmorphism aesthetics and improved interactive feedback.
- Updated `external_metrics` service for better multi-host data aggregation.
- Updated Docker build pipeline to only trigger on versioned tags (`v*`).

### Fixed
- Resolved over 1,200 linting and formatting issues across the codebase.
- Fixed numerous type-related bugs and inconsistencies identified by Mypy.
- Addressed various issues in webhook CRUD API and configuration validation.
- Fixed several edge cases in collector token refreshing and error handling.
