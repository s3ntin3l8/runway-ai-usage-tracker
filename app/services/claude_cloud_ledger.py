"""Reduce SDK objects to usage-only checkpoint records; never unwrap envelopes.

This is observational accounting. HMAC identities support duplicate comparisons
within one explicitly authorized job; they are never public report fields.
"""

from __future__ import annotations

import hmac
from typing import Any

from scripts.audit_claude_usage import MODEL_FIELDS, TOKEN_FIELDS


def _counters(value: object, mapping: dict[str, str]) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for destination, source in mapping.items():
        number = value.get(source)
        if type(number) is not int or not 0 <= number < 2**63:
            return None
        result[destination] = number
    return result


def _identity(value: object, key: bytes, domain: bytes) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 512:
        return None
    try:
        return hmac.digest(key, domain + b"\x00" + value.encode(), "sha256").hex()
    except UnicodeEncodeError:
        return None


def _model_usage(value: object, key: bytes) -> object:
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        return False
    total = dict.fromkeys(TOKEN_FIELDS, 0)
    models = {}
    for name, entry in value.items():
        identity = _identity(name, key, b"model")
        counters = _counters(entry, MODEL_FIELDS)
        if identity is None or identity in models or counters is None:
            return False
        models[identity] = {MODEL_FIELDS[field]: counters[field] for field in TOKEN_FIELDS}
        for field in TOKEN_FIELDS:
            total[field] += counters[field]
            if total[field] >= 2**63:
                return False
    # Preserve per-model reset evidence; the public SDK audit still emits totals only.
    return models


def usage_only_record(record: object, key: bytes) -> dict[str, Any] | None:
    """Keep fixed usage fields and pseudonymous message identity, or ignore it.

    Missing/invalid values remain unknown. Fixed invalid sentinels preserve the
    existing SDK auditor's diagnostic distinctions without retaining raw input.
    """
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("invalid_ledger_key")
    if not isinstance(record, dict):
        return None
    if record.get("type") == "assistant":
        raw = record.get("message")
        message = raw if isinstance(raw, dict) else {}
        identifier = message.get("id")
        digest = _identity(identifier, key, b"message")
        usage = _counters(message.get("usage"), {field: field for field in TOKEN_FIELDS})
        result: dict[str, Any] = {
            "type": "assistant",
            "message": {"id": digest, "usage": usage if usage is not None else False},
        }
        if record.get("parent_tool_use_id") is not None:
            result["parent_tool_use_id"] = True
        return result
    if record.get("type") == "result":
        raw_usage = record.get("usage")
        usage = _counters(raw_usage, {field: field for field in TOKEN_FIELDS})
        subtype = record.get("subtype")
        return {
            "type": "result",
            "subtype": (
                "error_observed"
                if isinstance(subtype, str) and subtype.startswith("error_")
                else "unclassified"
            ),
            "usage": usage if usage is not None else None if raw_usage is None else False,
            "modelUsage": _model_usage(record.get("modelUsage"), key),
        }
    return None


def _comparison_side(
    records: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, int]], set[str], int]:
    messages: dict[str, dict[str, int]] = {}
    conflicts: set[str] = set()
    unusable = 0
    for record in records:
        if record.get("type") != "assistant":
            continue
        if record.get("parent_tool_use_id") is not None:
            continue
        raw = record.get("message")
        message = raw if isinstance(raw, dict) else {}
        identifier = message.get("id")
        usage = _counters(message.get("usage"), {field: field for field in TOKEN_FIELDS})
        if not isinstance(identifier, str) or not identifier or usage is None:
            unusable += 1
            continue
        input_cache = {key: value for key, value in usage.items() if key != "output_tokens"}
        if identifier in messages and messages[identifier] != input_cache:
            conflicts.add(identifier)
        else:
            messages[identifier] = input_cache
    return messages, conflicts, unusable


def compare_usage_records(
    local: list[dict[str, Any]], cloud: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare expected input/cache per message; never infer consumption/parity.

    Both sides must already be reduced with the same job key. Extra cloud
    messages are reported; a matching subset is not complete-session parity.
    Conflicting identities never choose a first/last counter vector.
    """
    expected, local_conflicts, local_unusable = _comparison_side(local)
    observed, cloud_conflicts, cloud_unusable = _comparison_side(cloud)
    shared = expected.keys() & observed.keys()
    conflicts = local_conflicts | cloud_conflicts
    mismatches = sum(expected[key] != observed[key] for key in shared - conflicts)
    return {
        "local_messages": len(expected),
        "cloud_messages": len(observed),
        "matched_messages": len(shared),
        "missing_messages": len(expected.keys() - observed.keys()),
        "additional_cloud_messages": len(observed.keys() - expected.keys()),
        "mismatched_messages": mismatches,
        "local_conflicts": len(local_conflicts),
        "cloud_conflicts": len(cloud_conflicts),
        "local_unusable_records": local_unusable,
        "cloud_unusable_records": cloud_unusable,
        "all_expected_input_cache_match": bool(
            expected
            and len(shared) == len(expected)
            and not (mismatches or conflicts or local_unusable or cloud_unusable)
        ),
        "cloud_contract_verified": False,
        "ready_for_ingestion": False,
    }
