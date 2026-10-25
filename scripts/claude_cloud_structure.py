"""Fixed structural counters only: no dynamic keys, text, IDs or JSON-string decoding."""

from __future__ import annotations

from typing import Any, Literal

StructureLocation = Literal[
    "event",
    "data",
    "content",
    "message",
    "payload",
    "event.event",
    "data.message",
    "content.message",
    "event.event.message",
]
StructureType = Literal["missing", "null", "boolean", "number", "string", "array", "object"]
StructureCategory = Literal[
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
]
StructureField = Literal[
    "type", "event", "message", "data", "content", "payload", "usage", "modelUsage", "role"
]

LOCATION_PATHS: dict[StructureLocation, tuple[str, ...]] = {
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
VALUE_TYPES: tuple[StructureType, ...] = (
    "missing",
    "null",
    "boolean",
    "number",
    "string",
    "array",
    "object",
)
CATEGORIES: tuple[StructureCategory, ...] = (
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
FIELDS: tuple[StructureField, ...] = (
    "type",
    "event",
    "message",
    "data",
    "content",
    "payload",
    "usage",
    "modelUsage",
    "role",
)
_KNOWN_CATEGORIES = frozenset(CATEGORIES) - {"other", "missing", "non_string", "not_object"}
_MISSING = object()


def empty_structure() -> dict[str, Any]:
    """Return independent, complete fixed-key counters for an empty attempt."""
    return {
        "events": 0,
        "locations": {
            location: {
                "types": dict.fromkeys(VALUE_TYPES, 0),
                "categories": dict.fromkeys(CATEGORIES, 0),
                "fields": dict.fromkeys(FIELDS, 0),
            }
            for location in LOCATION_PATHS
        },
    }


def _value_type(value: Any) -> StructureType:
    if value is _MISSING:
        return "missing"
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is list:
        return "array"
    return "object"


def observe_structure(report: dict[str, Any], event: dict[str, Any]) -> None:
    """Inspect a fixed number of declared paths; never traverse or copy arbitrary data."""
    report["events"] += 1
    for location, path in LOCATION_PATHS.items():
        value: Any = event
        for field in path:
            value = value.get(field, _MISSING) if type(value) is dict else _MISSING
        row = report["locations"][location]
        row["types"][_value_type(value)] += 1
        category = "not_object"
        if type(value) is dict:
            raw = value.get("type", _MISSING)
            if raw is _MISSING:
                category = "missing"
            elif type(raw) is not str:
                category = "non_string"
            else:
                category = raw if raw in _KNOWN_CATEGORIES else "other"
            for field in FIELDS:
                row["fields"][field] += field in value
        row["categories"][category] += 1
