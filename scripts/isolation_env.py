"""Environment variables the test suite must never inherit from the operator.

Shared by the runner (``scripts/dev_checks.py``) and a bare ``pytest``
(``tests/conftest.py``) so the two cannot drift apart. Stdlib-only on purpose:
``dev_checks`` must import it even when the environment is broken.
"""

from __future__ import annotations

# Environment variables that point the app at filesystem state outside HOME.
LIVE_STATE_ENV_VARS: tuple[str, ...] = (
    "DATABASE_PATH",
    "GITHUB_OAUTH_PATH",
    "OPENCODE_DB_PATH",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "HERMES_HOME",
)
