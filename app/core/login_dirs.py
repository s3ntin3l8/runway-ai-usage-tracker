"""Directory-only Fleet settings, shared by the server and frozen sidecars.

Validation is lexical: only the sidecar expands or inspects a host path.
"""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

LOGIN_DIR_KEYS = ("claude_config_dirs", "codex_home")
_FILE_NAMES = {".credentials.json", ".claude.json", "auth.json", "statusline.json"}


def validate_directories(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("Expected at most 32 directories")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("Directories must be strings")
        if any(ord(c) < 32 or ord(c) == 127 for c in raw):
            raise ValueError("Directories cannot contain control characters")
        path = raw.strip()
        if not path or len(path) > 4096 or any(c in path for c in "*?["):
            raise ValueError("Expected a directory without glob patterns")
        posix, windows = PurePosixPath(path), PureWindowsPath(path)
        if not (
            posix.is_absolute()
            or windows.is_absolute()
            or path == "~"
            or path.startswith(("~/", "~\\"))
        ):
            raise ValueError("Use an absolute or home-relative directory")
        if windows.name.lower() in _FILE_NAMES:
            raise ValueError("Use the containing directory, not a credential file")
        if path not in result:
            result.append(path)
    return result


def clean_login_dirs(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError("Expected directory settings")
    return {key: validate_directories(value.get(key, [])) for key in LOGIN_DIR_KEYS}


def stored_login_dirs(value: str | None) -> dict[str, list[str]]:
    """Tolerate missing/corrupt old database values without touching paths."""
    import json

    try:
        return clean_login_dirs(json.loads(value)) if value else clean_login_dirs({})
    except (ValueError, TypeError):
        return clean_login_dirs({})
