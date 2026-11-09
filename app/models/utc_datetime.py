"""Custom Pydantic annotated type for UTC-aware datetime fields.

Every `datetime` column on a SQLModel table that gets serialized to JSON
(via `model_dump()` and FastAPI) goes out as a tz-aware ISO string with
explicit `+00:00` offset, so JavaScript's `new Date()` cannot misinterpret
it as browser-local time.

Use as `UTCDateTime` (or `UTCDateTime | None` for nullable columns).
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer


def _coerce_utc(v: object) -> object:
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=UTC)
    return v


def iso_utc(v: datetime | None) -> str | None:
    """Coerce a (possibly-naive) datetime to a UTC-marked ISO 8601 string.

    Use at any call site that emits a datetime to JSON without going through
    `model_dump()` — primarily ORM-hydrated `.isoformat()` calls in service
    layers, where the SQLite-stored value comes back naive.
    """
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.isoformat()


UTCDateTime = Annotated[
    datetime,
    BeforeValidator(_coerce_utc),
    PlainSerializer(iso_utc, return_type=str, when_used="always"),
]
