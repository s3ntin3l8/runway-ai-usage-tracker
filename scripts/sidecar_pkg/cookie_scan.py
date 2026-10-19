"""Internal store-read evidence. Credential values must never enter diagnostics."""

from dataclasses import dataclass, field
from typing import Literal

ScanStatus = Literal[
    "candidates", "empty", "missing", "inaccessible", "denied", "timed_out", "unsupported"
]


@dataclass
class StoreScan[T]:
    status: ScanStatus
    values: list[T] = field(default_factory=list, repr=False)
    expired_count: int = 0
    rejected_count: int = 0


def report_scan(browser: str, origin: str, result: StoreScan, outcomes: list[dict] | None) -> None:
    """Emit counts/statuses only; hash local store identity before reporting it."""
    import hashlib
    import logging

    summary = {
        "browser": browser,
        "store_id": hashlib.pbkdf2_hmac(
            "sha256", origin.encode(), b"runway-store-scan-v1", 1
        ).hex()[:24],
        "status": result.status,
        "candidate_count": len(result.values),
        "expired_count": result.expired_count,
        "rejected_count": result.rejected_count,
    }
    if outcomes is not None:
        outcomes.append(summary)
    logging.info("macOS cookie store scan: %s", summary)
