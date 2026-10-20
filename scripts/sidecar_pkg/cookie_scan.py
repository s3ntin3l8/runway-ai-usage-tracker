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
    logging.info("macOS cookie store scan: %s", summary)
