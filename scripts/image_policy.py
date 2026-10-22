"""Fail-closed release policy over unfiltered Grype reports."""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

KNOWN_SEVERITIES = frozenset({"Critical", "High", "Medium", "Low", "Negligible", "Unknown"})
BLOCKING_SEVERITIES = frozenset({"Critical", "High"})
# Upstream Anchore publishes the Grype vulnerability database about once a day.
# A release is built roughly every 24 hours, but its archive only reaches the
# CDN 50-60 minutes after the build stamp, and consecutive releases are built
# 23h50m-24h12m apart. The newest database anybody can download is therefore
# up to about 25 hours old by its build stamp, so a 24 hour ceiling is not
# satisfiable for roughly an hour every day. The audit uses the release
# upstream currently advertises (see check_database) and keeps this ceiling as
# the tripwire for an upstream feed that stopped publishing; the newest
# advertised release never reaches it while upstream keeps publishing daily.
MAX_DB_AGE = timedelta(hours=30)
MAX_EXCEPTION_AGE = timedelta(days=30)
# How close an exception may come to its expiry before the repository notices.
# The shipped document expires on a single horizon, so the tripwire has to fire
# before that horizon rather than when the image build fails. A week is enough
# to re-review the applicability rationale, open a pull request and merge it
# during working hours -- instead of discovering the lapse when an image build
# turns red on a Sunday evening.
EXPIRY_WARNING_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class ExceptionSet:
    """The exception document split by whether its review window is still open.

    ``active`` holds the entries that still waive a finding. ``expired`` holds
    the entries whose window closed: they waive nothing, so the findings they
    would have covered are reported as blocked. Keeping the two apart is what
    stops one lapsed entry from discarding the whole document.
    """

    active: dict[tuple[str, ...], dict[str, Any]] = field(default_factory=dict)
    expired: dict[tuple[str, ...], dict[str, Any]] = field(default_factory=dict)


def _describe(entry: dict[str, Any]) -> str:
    """Name an exception entry so an error points at the entry that caused it."""
    parts = [
        entry.get("id", "?"),
        entry.get("package", "?"),
        entry.get("version", "?"),
        entry.get("architecture", "?"),
    ]
    return " ".join(str(part) for part in parts)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed.astimezone(UTC)


def database_checksum(database: dict[str, Any]) -> str:
    """Return the checksum of the source archive the database was built from."""
    return parse_qs(urlparse(database.get("from", "")).query).get("checksum", [""])[0]


def check_database(
    database: dict[str, Any], now: datetime, expected_checksum: str | None = None
) -> None:
    # Record the checksummed source archive and validated build timestamp.
    built = timestamp(database["built"])
    checksum = database_checksum(database)
    if (
        database.get("error")
        or database.get("valid") is not True
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum)
    ):
        raise ValueError("Vulnerability database is incomplete")
    if built > now + timedelta(minutes=5) or now - built > MAX_DB_AGE:
        hours = int(MAX_DB_AGE.total_seconds() // 3600)
        raise ValueError(f"Vulnerability database must be at most {hours} hours old")
    # The scan must use the release upstream currently advertises, never a
    # database upstream has already superseded.
    if expected_checksum is not None and checksum != expected_checksum:
        raise ValueError("Vulnerability database is not the release upstream currently advertises")


def exception_key(entry: dict[str, Any]) -> tuple[str, ...]:
    return tuple(entry[k] for k in ("id", "namespace", "package", "version", "architecture"))


def renewal_due(loaded: ExceptionSet, now: datetime) -> list[dict[str, Any]]:
    """Entries whose review window has closed or is about to close.

    A lapsed entry applies to nothing and a closing one is about to stop
    applying, so both mean the document needs a renewal. The unit test and the
    scheduled check share this, so both agree on what "due" means.
    """
    due = list(loaded.expired.values())
    due.extend(
        entry
        for entry in loaded.active.values()
        if timestamp(entry["expires_at"]) - now < EXPIRY_WARNING_WINDOW
    )
    return due


def load_exceptions(path: Path, now: datetime) -> ExceptionSet:
    """Load the exception document, splitting it into live and lapsed entries.

    A malformed entry still fails the whole document: a broken document is a
    different failure class from a lapsed one, and silently downgrading it would
    hide the mistake. A lapsed entry, on the other hand, simply stops applying
    -- the finding it covered becomes unexcepted and blocks, and the rest of the
    document keeps working.
    """
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1 or not isinstance(data.get("exceptions"), list):
        raise ValueError("Invalid image exception document")
    active: dict[tuple[str, ...], dict[str, Any]] = {}
    expired: dict[tuple[str, ...], dict[str, Any]] = {}
    seen: set[tuple[str, ...]] = set()
    for entry in data["exceptions"]:
        name = _describe(entry)
        for field_name in (
            "id",
            "namespace",
            "package",
            "version",
            "architecture",
            "reason",
            "evidence",
            "owner",
            "issue",
            "reviewed_at",
            "expires_at",
        ):
            if not isinstance(entry.get(field_name), str) or not entry[field_name].strip():
                raise ValueError(f"Exception {name} requires {field_name}")
        if entry["architecture"] not in {"amd64", "arm64"}:
            raise ValueError(f"Exception {name} requires an exact supported architecture")
        if not entry["evidence"].startswith("https://") or not entry["issue"].startswith(
            "https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/"
        ):
            raise ValueError(f"Exception {name} requires HTTPS evidence and a Runway issue")
        try:
            reviewed, expires = timestamp(entry["reviewed_at"]), timestamp(entry["expires_at"])
        except ValueError as exc:
            raise ValueError(f"Exception {name} has an invalid timestamp: {exc}") from exc
        if reviewed > now:
            raise ValueError(
                f"Exception {name} was reviewed at {entry['reviewed_at']}, in the future"
            )
        window = expires - reviewed
        if not timedelta(0) < window <= MAX_EXCEPTION_AGE:
            raise ValueError(
                f"Exception {name} has a {window.days}-day review window, "
                f"which must be positive and at most {MAX_EXCEPTION_AGE.days} days"
            )
        key = exception_key(entry)
        if key in seen:
            raise ValueError(f"Duplicate image exception for {name}")
        seen.add(key)
        if expires <= now:
            expired[key] = entry
        else:
            active[key] = entry
    return ExceptionSet(active=active, expired=expired)


def evaluate(
    report: dict[str, Any],
    architecture: str,
    exceptions: ExceptionSet,
) -> dict[str, Any]:
    if architecture not in {"amd64", "arm64"} or not isinstance(report.get("matches"), list):
        raise ValueError("Missing architecture or vulnerability matches")
    blocked, accepted, python = [], [], []
    counts: Counter[str] = Counter()
    for match in report["matches"]:
        vulnerability, artifact = match["vulnerability"], match["artifact"]
        severity = vulnerability["severity"]
        if severity not in KNOWN_SEVERITIES:
            raise ValueError("Invalid vulnerability severity")
        counts[severity] += 1
        finding = {
            "id": vulnerability["id"],
            "namespace": vulnerability["namespace"],
            "package": artifact["name"],
            "version": artifact["version"],
            "architecture": architecture,
            "severity": severity,
            "fix_state": vulnerability.get("fix", {}).get("state", "unknown"),
        }
        if artifact.get("type") == "python":
            python.append(finding)
        if severity in BLOCKING_SEVERITIES:
            key = exception_key(finding)
            exception = exceptions.active.get(key)
            if exception:
                accepted.append({**finding, "exception": exception})
            else:
                lapsed = exceptions.expired.get(key)
                blocked.append(
                    {**finding, "expired_exception": _expired_note(lapsed)} if lapsed else finding
                )
    return {
        "architecture": architecture,
        "counts": dict(counts),
        "blocked": blocked,
        "exceptions": accepted,
        "python_findings": python,
        "passed": not blocked,
    }


def _expired_note(entry: dict[str, Any]) -> dict[str, Any]:
    """Explain why a finding is blocked: its exception lapsed rather than never existed."""
    return {
        "id": entry["id"],
        "package": entry["package"],
        "version": entry["version"],
        "architecture": entry["architecture"],
        "expires_at": entry["expires_at"],
        "reason": entry["reason"],
        "issue": entry["issue"],
    }
