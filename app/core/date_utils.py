from datetime import datetime
from typing import Any


def normalize_iso_date(iso_str: Any) -> Any:
    """
    Ensure an ISO date string is timezone-aware and uses 'Z' for UTC.
    Only modifies strings that appear to be ISO dates without timezone.
    """
    if not isinstance(iso_str, str) or not iso_str:
        return iso_str

    # If it's a T-date without offset or Z, append Z
    if "T" in iso_str and "Z" not in iso_str and "+" not in iso_str:
        return f"{iso_str}Z"

    return iso_str


def parse_iso8601_utc(value: str) -> datetime:
    """Parse an ISO 8601 string into an aware datetime.

    Accepts the 'Z' suffix that many provider APIs emit and that
    `datetime.fromisoformat` only learned to parse natively in Python 3.11.
    Keeping the explicit replace is defensive — it costs nothing and keeps the
    helper portable to older runtimes if the project ever supports them.

    Raises ValueError if the input is not a parseable ISO 8601 string.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
