"""Pure list-boundary diagnostics. Provider strings never leave this module."""

from __future__ import annotations

import hmac
import json
import re
from typing import Any

KINDS = ("missing", "null", "boolean", "number", "string", "array", "object", "not_object")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def job_digest(key: bytes, domain: bytes, value: object) -> str:
    """Existing checkpoint digest protocol: retain byte-for-byte compatibility."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hmac.digest(key, domain + b"\x00" + payload, "sha256").hex()


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "not_object"


def observe_list_boundary(
    payload: object, key: bytes, seen_sessions: set[str], seen_cursors: set[str]
) -> dict[str, Any]:
    """Inspect at most twenty entries. HMAC comparisons occur only in memory.

    cursor_supported is the frozen diagnostic-v1 shared sample-parser policy,
    not a provider guarantee or the verification-only optional-terminal adapter.
    A null/missing data value remains unknown rather than becoming an empty list.
    """
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("invalid_ledger_key")
    root = payload if isinstance(payload, dict) else {}
    data = root.get("data")
    rows = data if isinstance(data, list) else []
    kinds = dict.fromkeys(KINDS, 0)
    valid = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            kinds["not_object"] += 1
        elif "id" not in row:
            kinds["missing"] += 1
        else:
            value = row["id"]
            kinds[_kind(value)] += 1
            if isinstance(value, str) and SESSION_ID.fullmatch(value):
                valid.append(job_digest(key, b"listed-session", value))
    cursor = root.get("next_cursor")
    if "next_cursor" not in root:
        cursor_state = "missing"
    elif cursor is None:
        cursor_state = "null"
    elif cursor == "" and isinstance(cursor, str):
        cursor_state = "empty_string"
    elif (
        isinstance(cursor, str) and len(cursor) <= 2048 and all(ord(char) >= 32 for char in cursor)
    ):
        cursor_state = "nonempty_string"
    else:
        cursor_state = "invalid"
    supported = cursor_state in ("null", "nonempty_string")
    more = root.get("has_more")
    more_state = (
        "missing"
        if "has_more" not in root
        else "true"
        if more is True
        else "false"
        if more is False
        else "invalid"
    )
    return {
        "top_level_kind": _kind(payload),
        "data_kind": "not_object"
        if not isinstance(payload, dict)
        else _kind(data)
        if "data" in root
        else "missing",
        "record_count": len(rows) if isinstance(data, list) else None,
        "records_over_limit": len(rows) > 20,
        "checked_records": min(len(rows), 20),
        "id_kinds": kinds,
        "valid_ids": len(valid),
        "invalid_ids": min(len(rows), 20) - len(valid),
        "duplicate_valid_ids": len(valid) - len(set(valid)),
        "previously_seen_ids": sum(value in seen_sessions for value in valid),
        "cursor_state": cursor_state,
        "cursor_supported": supported,
        "cursor_repeated": job_digest(key, b"cursor", ["listing", cursor]) in seen_cursors
        if cursor_state == "nonempty_string"
        else None,
        "has_more_state": more_state,
    }


class BoundaryDecodeError(ValueError):
    """Only fixed categories/counts, never the decoder's value-bearing exception."""

    def __init__(self, category: str, duplicates: int, nonfinite: int):
        super().__init__("unsupported_json")
        self.category = category
        self.duplicates = duplicates
        self.nonfinite = nonfinite


def decode_boundary_json(body: bytes | bytearray) -> Any:
    """Retain the existing strict JSON policy while distinguishing its failures."""
    duplicates = 0
    nonfinite = 0

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicates
        result = {}
        for key, value in values:
            duplicates += key in result
            result[key] = value
        return result

    def constant(value: str) -> None:
        nonlocal nonfinite
        nonfinite += 1
        return

    try:
        value = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except UnicodeDecodeError:
        raise BoundaryDecodeError("encoding_error", duplicates, nonfinite) from None
    except ValueError:
        raise BoundaryDecodeError("invalid_json", duplicates, nonfinite) from None
    except RecursionError:
        raise BoundaryDecodeError("nesting_limit", duplicates, nonfinite) from None
    if duplicates or nonfinite:
        raise BoundaryDecodeError(
            "duplicate_keys" if duplicates else "nonfinite_values", duplicates, nonfinite
        )
    return value
