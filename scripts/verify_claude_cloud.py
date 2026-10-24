"""Validate a sanitized #669 evidence envelope on stdin, with no application or network I/O."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any, BinaryIO

MAX_BYTES = 256 * 1024
OUTCOMES = {
    "sample_complete",
    "sample_limited",
    "sample_not_found",
    "no_sessions",
    "busy",
    "source_unavailable",
    "source_ineligible",
    "unsupported_source",
    "source_changed",
    "expired",
    "identity_mismatch",
    "identity_unavailable",
    "rejected",
    "rate_limited",
    "unavailable",
    "timeout",
    "response_limit",
    "unsupported_contract",
}
SEQUENCE = {"numeric", "invalid", "duplicates", "gaps", "out_of_order"}
TOKENS = {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
ASSISTANT = {
    "records",
    "unique_messages",
    "duplicate_messages",
    "conflicting_messages",
    "subagent_records",
}
RESULT = {
    "records",
    "usage_observations",
    "model_usage_observations",
    "usage_decreases",
    "model_usage_decreases",
    "error_results",
}
DIAGNOSTICS = {
    "unsupported_record",
    "subagent_scope_excluded",
    "missing_assistant_identity",
    "invalid_assistant_usage",
    "conflicting_assistant_usage",
    "output_placeholder_divergence",
    "error_result_observed",
    "result_scope_unverified",
    "no_usage_observations",
    "missing_result_usage",
    "invalid_result_usage",
    "missing_result_model_usage",
    "invalid_result_model_usage",
}


class EvidenceError(ValueError):
    """Only fixed, safe diagnostics may leave this module."""


def _require(condition: bool) -> None:
    if not condition:
        raise EvidenceError("invalid_evidence")


def _keys(value: Any, fields: set[str]) -> None:
    _require(type(value) is dict and set(value) == fields)


def _integer(value: Any, maximum: int = 2**63 - 1) -> None:
    _require(type(value) is int and 0 <= value <= maximum)


def _choice(value: Any, options: set[str]) -> None:
    _require(type(value) is str and value in options)


def _counters(value: Any, fields: set[str]) -> None:
    _keys(value, fields)
    for number in value.values():
        _integer(number)


def _false_flags(value: dict[str, Any]) -> None:
    _require(value["cloud_contract_verified"] is False and value["ready_for_ingestion"] is False)


def _audit(value: Any) -> None:
    _keys(
        value,
        {
            "format_version",
            "status",
            "source_contract",
            "cloud_contract_verified",
            "ready_for_ingestion",
            "assistant_output_accounting",
            "assistant",
            "result",
            "diagnostics",
        },
    )
    _require(type(value["format_version"]) is int and value["format_version"] == 1)
    _false_flags(value)
    _choice(value["status"], {"incomplete", "observations_only"})
    _choice(value["source_contract"], {"sdk_style_jsonl"})
    _choice(value["assistant_output_accounting"], {"excluded_placeholder"})
    assistant, result = value["assistant"], value["result"]
    _keys(assistant, ASSISTANT | {"input_cache_observations"})
    _keys(result, RESULT | {"last_usage_observation", "last_model_usage_observation"})
    for key in ASSISTANT:
        _integer(assistant[key])
    for key in RESULT:
        _integer(result[key])
    if assistant["input_cache_observations"] is not None:
        _counters(assistant["input_cache_observations"], TOKENS - {"output_tokens"})
    for key in ("last_usage_observation", "last_model_usage_observation"):
        if result[key] is not None:
            _counters(result[key], TOKENS)
    for prefix in ("usage", "model_usage"):
        count = result[prefix + "_observations"]
        last = result["last_" + prefix + "_observation"]
        _require(last is None or count > 0)
        if value["status"] == "observations_only":
            _require(count == 0 or last is not None)
        # Incomplete final records may clear a previously valid observation.
        _require(result[prefix + "_decreases"] <= max(0, count - 1))
    diagnostics = value["diagnostics"]
    _require(type(diagnostics) is list and len(diagnostics) <= len(DIAGNOSTICS))
    for diagnostic in diagnostics:
        _choice(diagnostic, DIAGNOSTICS)
    _require(len(set(diagnostics)) == len(diagnostics))


def _report(value: Any) -> None:
    _keys(
        value,
        {
            "format_version",
            "outcome",
            "stage",
            "requests",
            "http_status",
            "account_verified",
            "organization_verified",
            "session_list_records",
            "session_list_complete",
            "history_pages",
            "history_events",
            "history_complete",
            "sequence",
            "observations",
            "cloud_contract_verified",
            "ready_for_ingestion",
        },
    )
    _require(type(value["format_version"]) is int and value["format_version"] == 1)
    _false_flags(value)
    _choice(value["outcome"], OUTCOMES)
    _choice(value["stage"], {"source", "profile", "sessions", "history", "complete"})
    for key in ("account_verified", "organization_verified"):
        _require(type(value[key]) is bool)
    for key in ("session_list_complete", "history_complete"):
        _require(value[key] is None or type(value[key]) is bool)
    for key, maximum in (
        ("requests", 5),
        ("session_list_records", 20),
        ("history_pages", 3),
        ("history_events", 150),
    ):
        _integer(value[key], maximum)
    if value["http_status"] is not None:
        _integer(value["http_status"], 599)
        _require(value["http_status"] >= 100)
    _counters(value["sequence"], SEQUENCE)
    observations = value["observations"]
    _require(type(observations) is list and len(observations) <= 3)
    locations = set()
    for observation in observations:
        _keys(observation, {"location", "report"})
        _choice(observation["location"], {"event", "data", "content"})
        _require(observation["location"] not in locations)
        locations.add(observation["location"])
        _audit(observation["report"])
        audit = observation["report"]
        assistant, result = audit["assistant"], audit["result"]
        _require(assistant["records"] + result["records"] <= value["history_events"])
        _require(assistant["unique_messages"] <= assistant["records"])
        _require(assistant["duplicate_messages"] <= assistant["records"])
        _require(assistant["conflicting_messages"] <= assistant["unique_messages"])
        _require(assistant["subagent_records"] <= assistant["records"])
        for key in ("usage_observations", "model_usage_observations", "error_results"):
            _require(result[key] <= result["records"])


def _usage_requires_review(observations: list[dict[str, Any]]) -> bool:
    if len(observations) != 1:
        return True
    audit = observations[0]["report"]
    assistant, result = audit["assistant"], audit["result"]
    if (
        audit["status"] != "observations_only"
        or any(result[key] for key in ("error_results", "usage_decreases", "model_usage_decreases"))
        or assistant["conflicting_messages"]
        or assistant["subagent_records"]
    ):
        return True
    if set(audit["diagnostics"]) - {"result_scope_unverified", "output_placeholder_divergence"}:
        return True
    return not (
        (assistant["unique_messages"] and assistant["input_cache_observations"] is not None)
        or (result["usage_observations"] and result["last_usage_observation"] is not None)
        or (
            result["model_usage_observations"]
            and result["last_model_usage_observation"] is not None
        )
    )


def _decision(status: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "format_version": 1,
        "status": status,
        "reasons": reasons,
        "provenance_verified": False,
        "live_acceptance_passed": False,
        "cloud_contract_verified": False,
        "ready_for_ingestion": False,
    }


def evaluate(value: Any) -> dict[str, Any]:
    """Accept only the version-1 allowlist; supplied provenance is never authenticated."""
    _keys(
        value, {"format_version", "source_commit", "image", "observed_at", "deployment", "report"}
    )
    _require(type(value["format_version"]) is int and value["format_version"] == 1)
    for key, pattern in (
        ("source_commit", r"[0-9a-f]{40}"),
        ("image", r"ghcr\.io/s3ntin3l8/runway@sha256:[0-9a-f]{64}"),
        ("observed_at", r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"),
    ):
        _require(type(value[key]) is str and re.fullmatch(pattern, value[key]) is not None)
    try:
        datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
    except ValueError:
        raise EvidenceError("invalid_evidence") from None
    _choice(value["deployment"], {"selected-production", "isolated-test"})
    report = value["report"]
    _report(report)
    reasons = []
    if report["outcome"] != "sample_complete":
        reasons.append("probe_not_complete")
    # The session list may be sampled; only the selected history chain must be complete.
    if report["stage"] != "complete" or not (
        report["account_verified"]
        and report["organization_verified"]
        and report["http_status"] == 200
        and report["history_complete"] is True
        and report["history_pages"] >= 1
        and report["history_events"] >= 1
        and report["session_list_records"] >= 1
        and report["requests"] == report["history_pages"] + 2
        and report["history_events"] <= report["history_pages"] * 50
    ):
        reasons.append("sample_requires_review")
    sequence = report["sequence"]
    if sequence["numeric"] != report["history_events"] or any(
        sequence[key] for key in SEQUENCE - {"numeric"}
    ):
        reasons.append("sequence_requires_review")
    if _usage_requires_review(report["observations"]):
        reasons.append("usage_requires_review")
    # JSON roundtrip returns independent, already-validated evidence.
    return {
        **_decision("stop" if reasons else "sample_observed", reasons),
        **{key: value[key] for key in ("source_commit", "image", "observed_at", "deployment")},
        "report": json.loads(json.dumps(report)),
    }


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _constant(value: str) -> None:
    raise EvidenceError("invalid_evidence")


def read_evidence(stream: BinaryIO) -> Any:
    """Bound bytes before decoding and reject duplicate keys/nonstandard constants."""
    raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise EvidenceError("input_limit_exceeded")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise EvidenceError("invalid_evidence") from None


def main() -> int:
    try:
        if len(sys.argv) != 1:
            raise EvidenceError("invalid_evidence")
        result = evaluate(read_evidence(sys.stdin.buffer))
    except EvidenceError as exc:
        reason = (
            "input_limit_exceeded" if str(exc) == "input_limit_exceeded" else "invalid_evidence"
        )
        result = _decision("stop", [reason])
    except (OSError, RecursionError):
        result = _decision("stop", ["invalid_evidence"])
    try:
        print(json.dumps(result, sort_keys=True, indent=2))
        sys.stdout.flush()
    except OSError:
        # Closing a broken buffered pipe can fail again. Mark it closed so
        # interpreter shutdown does not emit an unraisable flush traceback.
        try:
            sys.stdout.close()
        except OSError:
            # Closing marks the stream closed even if its buffered flush fails again.
            pass
        return 2
    return 0 if result["status"] == "sample_observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
