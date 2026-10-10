"""Fail-closed release policy over unfiltered Grype reports."""

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

KNOWN_SEVERITIES = frozenset({"Critical", "High", "Medium", "Low", "Negligible", "Unknown"})
BLOCKING_SEVERITIES = frozenset({"Critical", "High"})
MAX_DB_AGE = timedelta(hours=24)
MAX_EXCEPTION_AGE = timedelta(days=30)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed.astimezone(UTC)


def check_database(database: dict[str, Any], now: datetime) -> None:
    # Record the checksummed source archive and validated build timestamp.
    built = timestamp(database["built"])
    checksum = parse_qs(urlparse(database.get("from", "")).query).get("checksum", [""])[0]
    if (
        database.get("error")
        or database.get("valid") is not True
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum)
    ):
        raise ValueError("Vulnerability database is incomplete")
    if built > now + timedelta(minutes=5) or now - built > MAX_DB_AGE:
        raise ValueError("Vulnerability database must be at most 24 hours old")


def exception_key(entry: dict[str, Any]) -> tuple[str, ...]:
    return tuple(entry[k] for k in ("id", "namespace", "package", "version", "architecture"))


def load_exceptions(path: Path, now: datetime) -> dict[tuple[str, ...], dict[str, Any]]:
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1 or not isinstance(data.get("exceptions"), list):
        raise ValueError("Invalid image exception document")
    result = {}
    for entry in data["exceptions"]:
        for field in (
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
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"Exception requires {field}")
        if entry["architecture"] not in {"amd64", "arm64"}:
            raise ValueError("Exception requires an exact supported architecture")
        if not entry["evidence"].startswith("https://") or not entry["issue"].startswith(
            "https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/"
        ):
            raise ValueError("Exception requires HTTPS evidence and a Runway issue")
        reviewed, expires = timestamp(entry["reviewed_at"]), timestamp(entry["expires_at"])
        if (
            reviewed > now
            or expires <= now
            or not timedelta(0) < expires - reviewed <= MAX_EXCEPTION_AGE
        ):
            raise ValueError("Exception is expired or exceeds its 30-day review window")
        key = exception_key(entry)
        if key in result:
            raise ValueError("Duplicate image exception")
        result[key] = entry
    return result


def evaluate(
    report: dict[str, Any],
    architecture: str,
    exceptions: dict[tuple[str, ...], dict[str, Any]],
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
            exception = exceptions.get(exception_key(finding))
            if exception:
                accepted.append({**finding, "exception": exception})
            else:
                blocked.append(finding)
    return {
        "architecture": architecture,
        "counts": dict(counts),
        "blocked": blocked,
        "exceptions": accepted,
        "python_findings": python,
        "passed": not blocked,
    }
