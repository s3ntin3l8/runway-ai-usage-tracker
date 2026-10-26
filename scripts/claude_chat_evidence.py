"""Fixed ordinary Chat structure observations; never retain content or identities."""

from __future__ import annotations

from typing import Any
from uuid import UUID

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
_MISSING = object()
MAX_MESSAGES = 500


def empty_scope() -> dict[str, Any]:
    return {
        "records": 0,
        "shapes": dict.fromkeys(SHAPES, 0),
        "fields": {field: dict.fromkeys(COUNTER_SHAPES, 0) for field in FIELDS},
        "context_count_shapes": dict.fromkeys(COUNTER_SHAPES, 0),
    }


def empty_report() -> dict[str, Any]:
    return {
        "format_version": 1,
        "outcome": "source_unavailable",
        "stage": "source",
        "requests": 0,
        "http_status": None,
        "account_verified": False,
        "organization_verified": False,
        "target_verified": False,
        "history_complete": False,
        "messages": 0,
        "roles": dict.fromkeys(("human", "assistant", "unknown"), 0),
        "identities": dict.fromkeys(("present", "missing", "invalid", "duplicate"), 0),
        "usage": {"conversation": empty_scope(), "messages": empty_scope()},
        "exact_token_accounting_verified": False,
        "provenance_verified": False,
        "cloud_contract_verified": False,
        "ready_for_ingestion": False,
    }


def _shape(value: Any) -> str:
    if value is _MISSING:
        return "missing"
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if isinstance(value, dict):
        return "object" if value else "empty_object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


def _counter_shape(value: Any) -> str:
    if type(value) is int:
        if value == 0:
            return "zero"
        return "positive_integer" if 0 < value < 2**63 else "invalid_number"
    shape = _shape(value)
    return {"number": "invalid_number", "empty_object": "object"}.get(shape, shape)


def _observe_scope(scope: dict[str, Any], row: dict[str, Any]) -> None:
    scope["records"] += 1
    value = row.get("usage", _MISSING)
    scope["shapes"][_shape(value)] += 1
    for field in FIELDS:
        count = value.get(field, _MISSING) if isinstance(value, dict) else _MISSING
        scope["fields"][field][_counter_shape(count)] += 1
    scope["context_count_shapes"][_counter_shape(row.get("token_count", _MISSING))] += 1


def observe_conversation(payload: dict[str, Any]) -> dict[str, Any]:
    """Observe one already-target-verified response, making no completeness claim."""
    messages = payload.get("chat_messages")
    if (
        not isinstance(messages, list)
        or len(messages) > MAX_MESSAGES
        or any(not isinstance(row, dict) for row in messages)
    ):
        raise ValueError("unsupported_contract")
    report = empty_report()
    report["messages"] = len(messages)
    _observe_scope(report["usage"]["conversation"], payload)
    seen: set[str] = set()
    for row in messages:
        role = row.get("sender")
        report["roles"][role if role in ("human", "assistant") else "unknown"] += 1
        identity = row.get("uuid", _MISSING)
        if identity is _MISSING or identity is None:
            report["identities"]["missing"] += 1
        else:
            try:
                canonical = str(UUID(identity)) if isinstance(identity, str) else None
            except ValueError:
                canonical = None
            if canonical is None:
                report["identities"]["invalid"] += 1
            else:
                report["identities"]["present"] += 1
                report["identities"]["duplicate"] += canonical in seen
                seen.add(canonical)
        _observe_scope(report["usage"]["messages"], row)
    return report
