"""The window a sidecar's first run imports.

Both the collector loop and the MiniMax Code probe have to agree on it: the
collector reads ``SIDECAR_BOOTSTRAP_DAYS`` when it imports a provider for the
first time, and the probe reports what that import would cover. Two readers
of one env var are a drift waiting to happen, so the reading lives here.
"""

from __future__ import annotations

import os

ENV_VAR = "SIDECAR_BOOTSTRAP_DAYS"
DEFAULT_BOOTSTRAP_DAYS = 90


def bootstrap_window_days() -> int:
    """Return how many days of history a first run imports.

    An unset or unparsable value falls back to the default rather than
    failing the cycle: a typo in the environment must not stop the
    sidecar from collecting.
    """
    raw = os.getenv(ENV_VAR)
    if raw is None:
        return DEFAULT_BOOTSTRAP_DAYS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_BOOTSTRAP_DAYS
