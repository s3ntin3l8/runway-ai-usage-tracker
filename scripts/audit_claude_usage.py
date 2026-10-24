"""Audit SDK-style JSONL from stdin; emit usage observations, never consumed totals.

No cloud-envelope adapter, credential access, networking, or application imports.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator
from typing import Any, BinaryIO

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
MODEL_FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")


class AuditInputError(ValueError):
    """A fixed, sanitized input failure."""


def _counters(value: object, fields: tuple[str, ...] = TOKEN_FIELDS) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    numbers = [value.get(field) for field in fields]
    if any(type(number) is not int or not 0 <= number < 2**63 for number in numbers):
        return None
    return dict(zip(TOKEN_FIELDS, numbers, strict=True))


def _model_counters(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict) or not value:
        return None
    total = dict.fromkeys(TOKEN_FIELDS, 0)
    for entry in value.values():
        counters = _counters(entry, MODEL_FIELDS)
        if counters is None:
            return None
        for field in TOKEN_FIELDS:
            total[field] += counters[field]
    return total


def audit_messages(records: Iterable[object]) -> dict[str, Any]:
    """Compare observations without assuming result/reset/resume accounting scopes.

    IDs and model names remain in memory only. A conflicting repeated assistant
    ID invalidates the entire input/cache aggregate; neither first nor last wins.
    """
    seen: dict[str, dict[str, int]] = {}
    conflicts: set[str] = set()
    diagnostics: set[str] = set()
    assistant: dict[str, Any] = {
        "records": 0,
        "unique_messages": 0,
        "duplicate_messages": 0,
        "conflicting_messages": 0,
        "subagent_records": 0,
        "input_cache_observations": None,
    }
    result: dict[str, Any] = {
        "records": 0,
        "usage_observations": 0,
        "model_usage_observations": 0,
        "last_usage_observation": None,
        "last_model_usage_observation": None,
        "usage_decreases": 0,
        "model_usage_decreases": 0,
        "error_results": 0,
    }
    input_complete = True
    for index, record in enumerate(records):
        if index >= 100_000:
            raise AuditInputError("input_limit_exceeded")
        if not isinstance(record, dict):
            diagnostics.add("unsupported_record")
            continue
        kind = record.get("type")
        if kind == "assistant":
            assistant["records"] += 1
            if record.get("parent_tool_use_id") is not None:
                assistant["subagent_records"] += 1
                diagnostics.add("subagent_scope_excluded")
                continue
            message = record.get("message")
            identifier = message.get("id") if isinstance(message, dict) else None
            if not isinstance(identifier, str) or not identifier or len(identifier) > 512:
                diagnostics.add("missing_assistant_identity")
                input_complete = False
                continue
            counters = _counters(message.get("usage"))
            if counters is None:
                diagnostics.add("invalid_assistant_usage")
                input_complete = False
                continue
            if identifier in seen:
                assistant["duplicate_messages"] += 1
                if seen[identifier] != counters:
                    conflicts.add(identifier)
                    diagnostics.add("conflicting_assistant_usage")
                    input_complete = False
            else:
                seen[identifier] = counters
        elif kind == "result":
            result["records"] += 1
            subtype = record.get("subtype")
            if isinstance(subtype, str) and subtype.startswith("error_"):
                result["error_results"] += 1
                diagnostics.add("error_result_observed")
            diagnostics.add("result_scope_unverified")
            for source, destination, count, decreases, parse in (
                (
                    "usage",
                    "last_usage_observation",
                    "usage_observations",
                    "usage_decreases",
                    _counters,
                ),
                (
                    "modelUsage",
                    "last_model_usage_observation",
                    "model_usage_observations",
                    "model_usage_decreases",
                    _model_counters,
                ),
            ):
                counters = parse(record.get(source))
                previous = result[destination]
                if counters is None:
                    prefix = "missing" if record.get(source) is None else "invalid"
                    suffix = "usage" if source == "usage" else "model_usage"
                    diagnostics.add(f"{prefix}_result_{suffix}")
                else:
                    result[count] += 1
                    if previous is not None and any(
                        counters[field] < previous[field] for field in TOKEN_FIELDS
                    ):
                        result[decreases] += 1
                # An incomplete/error result never inherits a prior complete snapshot.
                result[destination] = counters
        elif kind not in ("user", "system", "stream_event"):
            diagnostics.add("unsupported_record")
    assistant["unique_messages"] = len(seen)
    assistant["conflicting_messages"] = len(conflicts)
    if seen and input_complete:
        assistant["input_cache_observations"] = {
            field: sum(value[field] for value in seen.values())
            for field in TOKEN_FIELDS
            if field != "output_tokens"
        }
    if not seen and not result["usage_observations"] and not result["model_usage_observations"]:
        diagnostics.add("no_usage_observations")
    incomplete = diagnostics - {"result_scope_unverified", "subagent_scope_excluded"}
    return {
        "format_version": 1,
        "status": "incomplete" if incomplete else "observations_only",
        "source_contract": "sdk_style_jsonl",
        "cloud_contract_verified": False,
        "ready_for_ingestion": False,
        "assistant_output_accounting": "excluded_placeholder",
        "assistant": assistant,
        "result": result,
        "diagnostics": sorted(diagnostics),
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuditInputError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AuditInputError("invalid_json")


def read_records(
    stream: BinaryIO,
    *,
    max_line_bytes: int = 1024 * 1024,
    max_bytes: int = 64 * 1024 * 1024,
    max_records: int = 100_000,
) -> Iterator[object]:
    """Bound memory/input and reject ambiguous JSON without reflecting its contents."""
    total = count = 0
    while line := stream.readline(max_line_bytes + 1):
        total += len(line)
        if len(line) > max_line_bytes or total > max_bytes:
            raise AuditInputError("input_limit_exceeded")
        if not line.strip():
            continue
        count += 1
        if count > max_records:
            raise AuditInputError("input_limit_exceeded")
        try:
            yield json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except UnicodeError:
            raise AuditInputError("invalid_encoding") from None
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            if isinstance(exc, AuditInputError):
                raise
            raise AuditInputError("invalid_json") from None


def main() -> int:
    """Read stdin only. Exit 2 for incomplete evidence or invalid input."""
    try:
        report = audit_messages(read_records(sys.stdin.buffer))
    except AuditInputError as exc:
        report = {
            "status": "incomplete",
            "ready_for_ingestion": False,
            "cloud_contract_verified": False,
            "diagnostics": [str(exc)],
        }
    except OSError:
        report = {
            "status": "incomplete",
            "ready_for_ingestion": False,
            "cloud_contract_verified": False,
            "diagnostics": ["input_unavailable"],
        }
    print(json.dumps(report, sort_keys=True))
    return 2 if report["status"] == "incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
