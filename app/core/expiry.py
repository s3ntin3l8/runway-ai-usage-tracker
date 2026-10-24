"""Read an expiry timestamp's unit off the number itself.

A credential's expiry may be published in seconds, milliseconds, microseconds or
nanoseconds. The registry maps an expiry field **by name** (``expiry_date``) and
never says which unit that field carries, so the reader has to work it out from
the magnitude of the value.

Both sides of the fence implement this rule — this module for the server, and
``scripts/sidecar_pkg/expiry.py`` for the sidecar — and
``tests/unit/test_mapping_resolver_parity.py`` pins the two together. The
server and the sidecar decide independently whether a credential is alive, so a
divergence between them means the two sides disagree about the same token.

The unit is read off the magnitude, not off the field name: a 2026 epoch is
~1.8e9 s, ~1.8e12 ms, ~1.8e15 us and ~1.8e18 ns, so each threshold sits clear
of the next. A millisecond value has to be ~3000 years in the future before it
would be read as microseconds, and it then lands in the past — expired, which
is the safe direction.

Every reader of an expiry in the server goes through here: the credential
provider's freshness probe and ``IdentityExtractor.exp_from_tokens``, which is
the single source of truth for the expiry of a *stored* bundle.
"""

from __future__ import annotations

import math
from typing import Any

#: The epoch-second magnitudes that tell an expiry's unit apart.
_EPOCH_MILLIS_THRESHOLD = 1e11
_EPOCH_MICROS_THRESHOLD = 1e14
_EPOCH_NANOS_THRESHOLD = 1e17

#: The last second of year 9999. An expiry beyond it is not a real expiry, so
#: it is unknown rather than fresh.
_MAX_EXPIRY_SECONDS = 253402300799.0


def expiry_seconds(value: Any) -> float | None:
    """Epoch seconds of *value*, normalising the unit it is published in.

    Returns None when *value* isn't a number, and when it is not a plausible
    expiry: an implausible value is unknown, never fresh.
    """
    try:
        moment = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(moment):
        # nan compares False against everything, so it would otherwise pass
        # every check below and be reported as an expiry of its own.
        return None
    if moment >= _EPOCH_NANOS_THRESHOLD:
        moment /= 1e9
    elif moment >= _EPOCH_MICROS_THRESHOLD:
        moment /= 1e6
    elif moment >= _EPOCH_MILLIS_THRESHOLD:
        moment /= 1e3
    if moment > _MAX_EXPIRY_SECONDS:
        # Past the last year this could name: not an expiry at all.
        return None
    return moment
