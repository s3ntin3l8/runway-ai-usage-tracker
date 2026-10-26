"""Validate a sanitized #669 evidence envelope on stdin, with no application or network I/O."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any, BinaryIO

# Deliberately mirrored: this verifier must also run as a standalone file with
# python -I -S. Round-trip contract tests catch drift from the producer vocabulary.
LOCATION_PATHS = {
    "event": (),
    "data": ("data",),
    "content": ("content",),
    "message": ("message",),
    "payload": ("payload",),
    "event.event": ("event",),
    "data.message": ("data", "message"),
    "content.message": ("content", "message"),
    "event.event.message": ("event", "message"),
}
VALUE_TYPES = ("missing", "null", "boolean", "number", "string", "array", "object")
CATEGORIES = (
    "assistant",
    "result",
    "user",
    "system",
    "tool_result",
    "tool_use",
    "stream_event",
    "message",
    "text",
    "thinking",
    "other",
    "missing",
    "non_string",
    "not_object",
)
FIELDS = ("type", "event", "message", "data", "content", "payload", "usage", "modelUsage", "role")

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


def _structure(value: Any, events: int) -> None:
    _keys(value, {"events", "locations"})
    _integer(value["events"], 150)
    _require(value["events"] == events)
    rows = value["locations"]
    _keys(rows, set(LOCATION_PATHS))
    for row in rows.values():
        _keys(row, {"types", "categories", "fields"})
        for name, keys in (("types", VALUE_TYPES), ("categories", CATEGORIES), ("fields", FIELDS)):
            _counters(row[name], set(keys))
            for count in row[name].values():
                _integer(count, events)
        _require(sum(row["types"].values()) == events)
        _require(sum(row["categories"].values()) == events)
        objects = row["types"]["object"]
        _require(row["categories"]["not_object"] == events - objects)
        _require(all(count <= objects for count in row["fields"].values()))
        _require(row["categories"]["missing"] == objects - row["fields"]["type"])
    _require(rows["event"]["types"]["object"] == events)
    for location, path in LOCATION_PATHS.items():
        if path:
            parent = next(key for key, parts in LOCATION_PATHS.items() if parts == path[:-1])
            _require(
                rows[location]["types"]["missing"] == events - rows[parent]["fields"][path[-1]]
            )


def _report(value: Any) -> None:
    _require(type(value) is dict)
    version = value.get("format_version")
    _require(type(version) is int and version in (1, 2))
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
        }
        | ({"structure"} if version == 2 else set()),
    )
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
    if version == 2:
        _structure(value["structure"], value["history_events"])
    observations = value["observations"]
    allowed_locations = {"event", "data", "content"} | ({"payload"} if version == 2 else set())
    _require(type(observations) is list and len(observations) <= len(allowed_locations))
    locations = set()
    for observation in observations:
        _keys(observation, {"location", "report"})
        _choice(observation["location"], allowed_locations)
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


HISTORY_COMPARISON = {
    "local_messages",
    "cloud_messages",
    "matched_messages",
    "missing_messages",
    "additional_cloud_messages",
    "mismatched_messages",
    "local_conflicts",
    "cloud_conflicts",
    "local_unusable_records",
    "cloud_unusable_records",
}
HISTORY_SCOPES = {
    "initializations",
    "session_scopes",
    "parent_scopes",
    "unknown_parent_records",
    "request_scopes",
    "record_versions",
    "record_version_changes",
    "scope_conflicts",
    "unknown_scope_records",
    "shared_session_scopes",
    "shared_parent_scopes",
}
HISTORY_OUTCOMES = (
    OUTCOMES - {"sample_complete", "sample_limited", "sample_not_found", "busy"}
) | {
    "pending",
    "complete",
    "cancelled",
    "interrupted",
    "limit_reached",
    "target_not_found",
}


def _history_comparison(value: Any) -> None:
    _keys(
        value,
        HISTORY_COMPARISON
        | {
            "all_expected_input_cache_match",
            "cloud_contract_verified",
            "ready_for_ingestion",
        },
    )
    _false_flags(value)
    for name in HISTORY_COMPARISON:
        _integer(value[name], 100_000)
    _require(type(value["all_expected_input_cache_match"]) is bool)
    shared = value["matched_messages"]
    _require(shared + value["missing_messages"] == value["local_messages"])
    _require(shared + value["additional_cloud_messages"] == value["cloud_messages"])
    _require(value["mismatched_messages"] <= shared)
    if value["all_expected_input_cache_match"]:
        _require(value["local_messages"] > 0 and value["missing_messages"] == 0)
        _require(
            not any(
                value[name]
                for name in (
                    "mismatched_messages",
                    "local_conflicts",
                    "cloud_conflicts",
                    "local_unusable_records",
                    "cloud_unusable_records",
                )
            )
        )


def _history_report(value: Any) -> None:
    """Strict complete-chain projection. Transport handles must be removed first."""
    _keys(
        value,
        {
            "format_version",
            "started_at",
            "deadline",
            "last_verified_at",
            "phase",
            "outcome",
            "requests",
            "accepted_response_bytes",
            "http_status",
            "account_verified",
            "organization_verified",
            "listed_sessions",
            "list_pages",
            "list_complete",
            "history_pages",
            "history_events",
            "history_complete",
            "sequence",
            "audit",
            "comparison",
            "subagent_comparison",
            "scope_observations",
            "declared_category",
            "execution_kind",
            "cloud_contract_verified",
            "ready_for_ingestion",
            "live_acceptance_passed",
            "provenance_verified",
        }
        | ({"rejected_list"} if type(value) is dict and "rejected_list" in value else set())
        # Reports from builds before the field existed stay valid.
        | ({"environment_kind"} if type(value) is dict and "environment_kind" in value else set()),
    )
    rejected = value.get("rejected_list")
    if rejected is not None:
        _rejected_list(rejected)
        _require(value["outcome"] == "unsupported_contract" and value["phase"] == "stopped")
        _require(value["account_verified"] and value["organization_verified"])
        _require(not value["list_complete"] and value["history_pages"] == 0)
    _require(type(value["format_version"]) is int and value["format_version"] == 1)
    _false_flags(value)
    _require(value["live_acceptance_passed"] is False and value["provenance_verified"] is False)
    for name in ("started_at", "deadline", "last_verified_at"):
        number = value[name]
        if name == "last_verified_at" and number is None:
            continue
        _require(type(number) in (int, float) and 0 <= number < 253402300800)
    _require(0 < value["deadline"] - value["started_at"] <= 1800)
    if value["last_verified_at"] is not None:
        _require(value["started_at"] <= value["last_verified_at"] < value["deadline"])
    _choice(value["phase"], {"listing", "history", "complete", "stopped"})
    _choice(value["outcome"], HISTORY_OUTCOMES)
    for name, maximum in (
        ("requests", 512),
        ("accepted_response_bytes", 256 * 1024 * 1024),
        ("listed_sessions", 20_000),
        ("list_pages", 512),
        ("history_pages", 512),
        ("history_events", 100_000),
    ):
        _integer(value[name], maximum)
    for name in ("account_verified", "organization_verified", "list_complete", "history_complete"):
        _require(type(value[name]) is bool)
    _require(not value["organization_verified"] or value["account_verified"])
    if value["http_status"] is not None:
        _integer(value["http_status"], 599)
        _require(value["http_status"] >= 100)
    _require(value["list_pages"] + value["history_pages"] <= value["requests"])
    _require(value["history_events"] <= value["history_pages"] * 50)
    _require(value["listed_sessions"] <= value["list_pages"] * 20)
    _counters(value["sequence"], SEQUENCE)
    _require(all(n <= value["history_events"] for n in value["sequence"].values()))
    _require(value["sequence"]["numeric"] + value["sequence"]["invalid"] == value["history_events"])
    if value["outcome"] == "complete":
        _require(
            value["phase"] == "complete" and value["list_complete"] and value["history_complete"]
        )
        _require(value["account_verified"] and value["organization_verified"])
    if value["history_complete"]:
        _require(value["list_complete"] and value["history_pages"] > 0)
    _audit(value["audit"])
    _require(
        value["audit"]["assistant"]["records"] + value["audit"]["result"]["records"]
        <= value["history_events"]
    )
    for name in ("comparison", "subagent_comparison"):
        _history_comparison(value[name])
    _counters(value["scope_observations"], HISTORY_SCOPES)
    _require(all(n <= value["history_events"] for n in value["scope_observations"].values()))
    _choice(
        value["declared_category"],
        {
            "cli_code",
            "desktop_code",
            "remote_code",
            "cowork",
            "ordinary_chat",
            "mobile_only",
            "unknown",
        },
    )
    _choice(value["execution_kind"], {"anthropic_cloud", "bridge", "other", "unknown"})
    if "environment_kind" in value:
        kind, execution = value["environment_kind"], value["execution_kind"]
        _require(
            kind is None
            or (type(kind) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", kind) is not None)
        )
        # The two fields are defined jointly: a label exists unless the kind is unknown,
        # a known kind is its own label, and `other` is any other label.
        _require((kind is None) == (execution == "unknown"))
        _require(execution not in {"anthropic_cloud", "bridge"} or kind == execution)
        _require(execution != "other" or kind not in {"anthropic_cloud", "bridge"})


def _history_decision(value: Any) -> dict[str, Any]:
    report = value["report"]
    _history_report(report)
    reasons = []
    if (
        report["outcome"] != "complete"
        or not report["history_complete"]
        or not report["list_complete"]
    ):
        reasons.append("history_not_complete")
    if report["sequence"]["numeric"] != report["history_events"] or any(
        report["sequence"][name] for name in SEQUENCE - {"numeric"}
    ):
        reasons.append("sequence_requires_review")
    # Complete pagination alone proves neither usage semantics nor source coverage.
    reasons.extend(
        ["accounting_requires_review", "coverage_requires_review", "provenance_requires_review"]
    )
    return {
        **_decision("stop", reasons),
        **{key: value[key] for key in ("source_commit", "image", "observed_at", "deployment")},
        "report": json.loads(json.dumps(report)),
    }


BOUNDARY_KINDS = {"missing", "null", "boolean", "number", "string", "array", "object", "not_object"}
BOUNDARY_OUTCOMES = {
    "observed",
    "busy",
    "checkpoint_unavailable",
    "checkpoint_ineligible",
    "checkpoint_changed",
    "source_changed",
    "identity_mismatch",
    "identity_unavailable",
    "expired",
    "rejected",
    "rate_limited",
    "timeout",
    "unavailable",
    "unsupported_contract",
    "response_limit",
}


def _boundary_observation(value: Any) -> None:
    _keys(
        value,
        {
            "top_level_kind",
            "data_kind",
            "record_count",
            "records_over_limit",
            "checked_records",
            "id_kinds",
            "valid_ids",
            "invalid_ids",
            "duplicate_valid_ids",
            "previously_seen_ids",
            "cursor_state",
            "cursor_supported",
            "cursor_repeated",
            "has_more_state",
        },
    )
    for name in ("top_level_kind", "data_kind"):
        _choice(value[name], BOUNDARY_KINDS)
    records = value["record_count"]
    if records is not None:
        _integer(records)
    for name in (
        "checked_records",
        "valid_ids",
        "invalid_ids",
        "duplicate_valid_ids",
        "previously_seen_ids",
    ):
        _integer(value[name], 20)
    _counters(value["id_kinds"], BOUNDARY_KINDS)
    _require(sum(value["id_kinds"].values()) == value["checked_records"])
    _require(value["checked_records"] == (min(records, 20) if records is not None else 0))
    _require((value["data_kind"] == "array") == (records is not None))
    _require(
        type(value["records_over_limit"]) is bool
        and value["records_over_limit"] == (records is not None and records > 20)
    )
    _require(value["valid_ids"] + value["invalid_ids"] == value["checked_records"])
    _require(value["valid_ids"] <= value["id_kinds"]["string"])
    _require(value["duplicate_valid_ids"] <= max(0, value["valid_ids"] - 1))
    _require(value["previously_seen_ids"] <= value["valid_ids"])
    _choice(
        value["cursor_state"], {"missing", "null", "empty_string", "nonempty_string", "invalid"}
    )
    _require(
        type(value["cursor_supported"]) is bool
        and value["cursor_supported"] == (value["cursor_state"] in ("null", "nonempty_string"))
    )
    if value["cursor_state"] == "nonempty_string":
        _require(type(value["cursor_repeated"]) is bool)
    else:
        _require(value["cursor_repeated"] is None)
    _choice(value["has_more_state"], {"missing", "true", "false", "invalid"})
    _require(value["top_level_kind"] == "object" or value["data_kind"] == "not_object")


def _rejected_list(value: Any) -> None:
    _keys(
        value,
        {
            "boundary",
            "response_encoding",
            "json_state",
            "duplicate_json_keys",
            "nonfinite_json_values",
        },
    )
    _choice(value["response_encoding"], {"identity", "unsupported"})
    _choice(
        value["json_state"],
        {
            "not_attempted",
            "decoded",
            "duplicate_keys",
            "nonfinite_values",
            "invalid_json",
            "encoding_error",
            "nesting_limit",
        },
    )
    _integer(value["duplicate_json_keys"])
    _integer(value["nonfinite_json_values"])
    state = value["json_state"]
    _require((value["boundary"] is not None) == (state == "decoded"))
    _require((state == "not_attempted") == (value["response_encoding"] == "unsupported"))
    _require(state == "not_attempted" or value["response_encoding"] == "identity")
    if state in {"not_attempted", "decoded"}:
        _require(value["duplicate_json_keys"] == value["nonfinite_json_values"] == 0)
    if state == "duplicate_keys":
        _require(value["duplicate_json_keys"] > 0)
    if state == "nonfinite_values":
        _require(value["nonfinite_json_values"] > 0)
    if value["boundary"] is not None:
        _boundary_observation(value["boundary"])


def _boundary_report(value: Any) -> None:
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
            "original_list_pages",
            "original_listed_sessions",
            "boundary",
            "response_encoding",
            "json_state",
            "duplicate_json_keys",
            "nonfinite_json_values",
            "cloud_contract_verified",
            "ready_for_ingestion",
            "live_acceptance_passed",
            "provenance_verified",
        },
    )
    _require(type(value["format_version"]) is int and value["format_version"] == 1)
    _false_flags(value)
    _require(value["live_acceptance_passed"] is False and value["provenance_verified"] is False)
    _choice(value["outcome"], BOUNDARY_OUTCOMES)
    _choice(value["stage"], {"checkpoint", "source", "profile", "sessions", "complete"})
    _integer(value["requests"], 2)
    pages, sessions = value["original_list_pages"], value["original_listed_sessions"]
    _require((pages is None) == (sessions is None))
    if pages is not None:
        _integer(pages, 512)
        _integer(sessions, 20000)
        _require(sessions <= pages * 20)
    for name in ("account_verified", "organization_verified"):
        _require(type(value[name]) is bool)
    if value["http_status"] is not None:
        _integer(value["http_status"], 599)
        _require(value["http_status"] >= 100)
    _choice(value["response_encoding"], {"unknown", "identity", "unsupported"})
    _choice(
        value["json_state"],
        {
            "not_attempted",
            "decoded",
            "duplicate_keys",
            "nonfinite_values",
            "invalid_json",
            "encoding_error",
            "nesting_limit",
        },
    )
    for name in ("duplicate_json_keys", "nonfinite_json_values"):
        _integer(value[name])
    if value["json_state"] in ("not_attempted", "decoded"):
        _require(value["duplicate_json_keys"] == value["nonfinite_json_values"] == 0)
    if value["json_state"] != "not_attempted":
        _require(value["response_encoding"] == "identity")
    if value["json_state"] == "duplicate_keys":
        _require(value["duplicate_json_keys"] > 0)
    if value["json_state"] == "nonfinite_values":
        _require(value["nonfinite_json_values"] > 0)
    observed = value["outcome"] == "observed"
    _require((value["boundary"] is not None) == observed)
    if observed:
        _require(pages is not None)
        _require(
            value["stage"] == "complete"
            and value["requests"] == 2
            and value["http_status"] == 200
            and value["account_verified"]
            and value["organization_verified"]
        )
        _require(value["json_state"] == "decoded" and value["response_encoding"] == "identity")
        _boundary_observation(value["boundary"])


def _boundary_decision(value: Any) -> dict[str, Any]:
    _boundary_report(value["report"])
    return {
        **_decision(
            "stop",
            [
                "list_contract_requires_review",
                "accounting_requires_review",
                "coverage_requires_review",
                "provenance_requires_review",
            ],
        ),
        **{key: value[key] for key in ("source_commit", "image", "observed_at", "deployment")},
        "report": json.loads(json.dumps(value["report"])),
    }


def evaluate(value: Any) -> dict[str, Any]:
    """Validate sample/history envelopes; supplied provenance is never authenticated."""
    _keys(
        value, {"format_version", "source_commit", "image", "observed_at", "deployment", "report"}
    )
    _require(type(value["format_version"]) is int and value["format_version"] in (1, 2, 3))
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
    if value["format_version"] == 3:
        return _boundary_decision(value)
    if value["format_version"] == 2:
        return _history_decision(value)
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
