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
