from typing import Any


def truncate_string(s: Any, limit: int = 40) -> str:
    """Standardize string truncation with ellipsis."""
    str_val = str(s)
    if len(str_val) <= limit:
        return str_val
    return str_val[: limit - 3] + "..."
