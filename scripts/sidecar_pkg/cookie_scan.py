"""Internal store-read evidence. Credential values must never enter diagnostics."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Literal

_DIAGNOSTIC_KEY = secrets.token_bytes(32)

ScanStatus = Literal[
    "candidates", "empty", "missing", "inaccessible", "denied", "timed_out", "unsupported"
]


@dataclass
class StoreScan[T]:
    status: ScanStatus
    values: list[T] = field(default_factory=list, repr=False)
    expired_count: int = 0
    rejected_count: int = 0
    schema_version: int | None = None


def report_scan(browser: str, origin: str, result: StoreScan, outcomes: list[dict] | None) -> None:
    """Emit internal diagnostics with process-local store correlation only."""
    import logging

    summary = {
        "browser": browser,
        "store_id": hmac.new(_DIAGNOSTIC_KEY, origin.encode(), hashlib.sha256).hexdigest()[:24],
        "status": result.status,
        "candidate_count": len(result.values),
        "expired_count": result.expired_count,
        "rejected_count": result.rejected_count,
    }
    if result.schema_version is not None:
        summary["schema_version"] = result.schema_version
    if outcomes is not None:
        outcomes.append(summary)
    logging.debug("macOS cookie store scan: %s", summary)


def report_scan_summary(outcomes: list[dict]) -> None:
    """Keep normal logs bounded to one count-only line per collection call."""
    import logging
    from collections import Counter

    logging.info(
        "macOS cookie scan summary: scans=%d statuses=%s candidates=%d expired=%d rejected=%d",
        len(outcomes),
        dict(sorted(Counter(item["status"] for item in outcomes).items())),
        sum(item["candidate_count"] for item in outcomes),
        sum(item["expired_count"] for item in outcomes),
        sum(item["rejected_count"] for item in outcomes),
    )
