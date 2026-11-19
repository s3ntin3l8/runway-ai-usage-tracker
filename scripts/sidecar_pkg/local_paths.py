"""Local sidecar paths shared by background collection and foreground authorization."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any

_LOGIN_DIR_CONFIG_KEYS = {"CLAUDE_CONFIG_DIR": "claude_config_dirs", "CODEX_HOME": "codex_home"}
_LOGIN_DIR_EXTRAS: dict[str, list[str]] = {}


def get_sidecar_dir() -> Path:
    """Get the sidecar configuration directory.

    Honours RUNWAY_CONFIG_DIR for parity with the server (set via .env in
    dev) so `make dev` and `make sidecar` share the same project-local
    config when invoked from the repo. Falls back to the platform default
    otherwise.
    """
    override = os.getenv("RUNWAY_CONFIG_DIR")
    if override:
        return Path(override) / "sidecar"
    if platform.system() == "Windows":
        app_data = os.getenv("APPDATA")
        if app_data:
            return Path(app_data) / "runway" / "sidecar"
        return Path.home() / "AppData" / "Roaming" / "runway" / "sidecar"
    return Path.home() / ".config" / "runway" / "sidecar"


def get_config_path() -> Path:
    """Return the local sidecar configuration file."""
    return get_sidecar_dir() / "config.json"


def _split_dirs(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list | tuple):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def configure_login_dirs(config: dict[str, Any]) -> None:
    """Pick up the per-CLI extra login dirs from the sidecar's local config."""
    _LOGIN_DIR_EXTRAS.clear()
    for env_name, key in _LOGIN_DIR_CONFIG_KEYS.items():
        dirs = _split_dirs(config.get(key))
        if dirs:
            _LOGIN_DIR_EXTRAS[env_name] = dirs


def local_keychain_login_dirs() -> list[Path]:
    """Only local env/config can authorize additional Claude Keychain targets."""
    values = [
        *_split_dirs(os.getenv("CLAUDE_CONFIG_DIR", "")),
        *_LOGIN_DIR_EXTRAS.get("CLAUDE_CONFIG_DIR", []),
    ]
    return list(dict.fromkeys(Path(os.path.expanduser(value)).resolve() for value in values))
