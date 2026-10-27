"""Validate sanitized Chat fixture evidence on stdin, independently and offline."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any

# Deliberately mirrored: this file runs alone with python -I -S. Contract tests
# compare its vocabulary and invariants with the producer/Pydantic schema.
FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
SHAPES = ("missing", "null", "empty_object", "object", "array", "string", "boolean", "number")
COUNTER_SHAPES = (
    "missing",
    "null",
    "zero",
    "positive_integer",
    "invalid_number",
    "boolean",
    "string",
    "array",
    "object",
)
OUTCOMES = (
    "observed",
    "busy",
    "source_unavailable",
    "source_ineligible",
    "unsupported_source",
    "source_changed",
    "expired",
    "identity_mismatch",
    "identity_unavailable",
    "organization_mismatch",
    "target_mismatch",
    "challenge",
    "rejected",
    "rate_limited",
    "unavailable",
    "timeout",
    "response_limit",
    "unsupported_contract",
)
STAGES = ("source", "account", "organizations", "conversation", "complete")
FALSE_FIELDS = (
    "history_complete",
    "exact_token_accounting_verified",
    "provenance_verified",
    "cloud_contract_verified",
    "ready_for_ingestion",
)
REPORT_FIELDS = (
    "format_version",
    "outcome",
    "stage",
    "requests",
    "http_status",
    "account_verified",
    "organization_verified",
    "target_verified",
    "messages",
    "roles",
    "identities",
    "usage",
    *FALSE_FIELDS,
)
ENVELOPE_FIELDS = (
    "format_version",
    "source_commit",
    "image",
    "observed_at",
    "deployment",
    "report",
)
MAX_BYTES = 64 * 1024


class EvidenceError(ValueError):
    """Only fixed diagnostic categories may cross this boundary."""


def _object(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys):
        raise EvidenceError("invalid_evidence")
    return value


def _count(value: Any, maximum: int = 500) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise EvidenceError("invalid_evidence")
    return value


def _histogram(value: Any, keys: tuple[str, ...], records: int) -> dict[str, int]:
    data = _object(value, keys)
    if sum(_count(number) for number in data.values()) != records:
        raise EvidenceError("invalid_evidence")
    return data


def _scope(value: Any) -> dict[str, Any]:
    data = _object(value, ("records", "shapes", "fields", "context_count_shapes"))
    records = _count(data["records"])
    shapes = _histogram(data["shapes"], SHAPES, records)
    fields = _object(data["fields"], FIELDS)
    for field in FIELDS:
        histogram = _histogram(fields[field], COUNTER_SHAPES, records)
        if histogram["missing"] < records - shapes["object"]:
            raise EvidenceError("invalid_evidence")
    _histogram(data["context_count_shapes"], COUNTER_SHAPES, records)
    return data


def validate_report(value: Any) -> dict[str, Any]:
    report = _object(value, REPORT_FIELDS)
    if type(report["format_version"]) is not int or report["format_version"] != 1:
        raise EvidenceError("invalid_evidence")
    if report["outcome"] not in OUTCOMES or report["stage"] not in STAGES:
        raise EvidenceError("invalid_evidence")
    if any(report[field] is not False for field in FALSE_FIELDS):
        raise EvidenceError("invalid_evidence")
    requests = _count(report["requests"], 3)
    http = report["http_status"]
    if http is not None and (type(http) is not int or not 100 <= http <= 599):
        raise EvidenceError("invalid_evidence")
    if requests == 0 and http is not None:
        raise EvidenceError("invalid_evidence")
    flags = ("account_verified", "organization_verified", "target_verified")
    if any(type(report[field]) is not bool for field in flags):
        raise EvidenceError("invalid_evidence")
    messages = _count(report["messages"])
    _histogram(report["roles"], ("human", "assistant", "unknown"), messages)
    identities = _object(report["identities"], ("present", "missing", "invalid", "duplicate"))
    for number in identities.values():
        _count(number)
    if sum(identities[key] for key in ("present", "missing", "invalid")) != messages:
        raise EvidenceError("invalid_evidence")
    if identities["duplicate"] > identities["present"]:
        raise EvidenceError("invalid_evidence")
    usage = _object(report["usage"], ("conversation", "messages"))
    for scope in usage.values():
        _scope(scope)
    if usage["messages"]["records"] != messages:
        raise EvidenceError("invalid_evidence")
    if report["account_verified"] and requests < 1:
        raise EvidenceError("invalid_evidence")
    if report["organization_verified"] and (not report["account_verified"] or requests < 2):
        raise EvidenceError("invalid_evidence")
    if report["outcome"] == "observed":
        if not (
            report["stage"] == "complete"
            and requests == 3
            and http == 200
            and all(report[field] for field in flags)
            and usage["conversation"]["records"] == 1
        ):
            raise EvidenceError("invalid_evidence")
    elif report["target_verified"] or messages or usage["conversation"]["records"]:
        raise EvidenceError("invalid_evidence")
    elif report["stage"] == "complete":
        raise EvidenceError("invalid_evidence")
    if report["outcome"] in ("source_changed", "source_unavailable", "timeout") and (
        report["account_verified"] or report["organization_verified"] or report["stage"] != "source"
    ):
        raise EvidenceError("invalid_evidence")
    return report


def _decision(visible: bool, reasons: list[str]) -> dict[str, Any]:
    return {
        "format_version": 1,
        "status": "stop",
        "fixture_visible": visible,
        "reasons": reasons,
        "exact_token_accounting_verified": False,
        "provenance_verified": False,
        "cloud_contract_verified": False,
        "live_acceptance_passed": False,
        "ready_for_ingestion": False,
    }


def evaluate(value: Any) -> dict[str, Any]:
    body = _object(value, ENVELOPE_FIELDS)
    if type(body["format_version"]) is not int or body["format_version"] != 1:
        raise EvidenceError("invalid_evidence")
    if not isinstance(body["source_commit"], str) or not re.fullmatch(
        r"[a-f0-9]{40}", body["source_commit"]
    ):
        raise EvidenceError("invalid_evidence")
    if not isinstance(body["image"], str) or not re.fullmatch(
        r"ghcr\.io/s3ntin3l8/runway@sha256:[a-f0-9]{64}", body["image"]
    ):
        raise EvidenceError("invalid_evidence")
    stamp = body["observed_at"]
    if not isinstance(stamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp
    ):
        raise EvidenceError("invalid_evidence")
    try:
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise EvidenceError("invalid_evidence") from None
    if body["deployment"] not in ("selected-production", "isolated-test"):
        raise EvidenceError("invalid_evidence")
    report = validate_report(body["report"])
    visible = report["outcome"] == "observed"
    reasons = [] if visible else ["fixture_not_verified"]
    reasons.extend(
        (
            "chat_accounting_unverified",
            "chat_history_completeness_unverified",
            "phone_only_provenance_unverified",
        )
    )
    result = _decision(visible, reasons)
    result.update(
        {key: body[key] for key in ("source_commit", "image", "observed_at", "deployment")}
    )
    result["report"] = report
    return result


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("invalid_evidence")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise EvidenceError("invalid_evidence")


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise EvidenceError("invalid_evidence")
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise EvidenceError("input_limit_exceeded")
        result = evaluate(
            json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_constant)
        )
    except (ValueError, RecursionError, OSError) as exc:
        reason = (
            "input_limit_exceeded"
            if isinstance(exc, EvidenceError) and str(exc) == "input_limit_exceeded"
            else "invalid_evidence"
        )
        result = _decision(False, [reason])
    try:
        print(json.dumps(result, sort_keys=True, indent=2))
        sys.stdout.flush()
    except OSError:
        try:
            sys.stdout.close()
        except OSError:
            # A closed downstream pipe cannot receive another diagnostic. Keep
            # the fixed failure exit instead of retrying output or leaking errors.
            pass
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
