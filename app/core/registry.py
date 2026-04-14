import json
import logging
import os
from typing import Any

from app.core.config import get_platform_config_dir, get_platform_data_dir

logger = logging.getLogger(__name__)


class Registry:
    """
    Centralized registry for AI provider extraction rules.
    Loads from app/core/registry.json on each call so the file can be edited
    without restarting the server.
    """

    def __init__(self):
        self._path = os.path.join(os.path.dirname(__file__), "registry.json")
        self._cache: dict[str, Any] = {}
        self._mtime: float = 0.0

    def _load(self) -> dict[str, Any]:
        """Load registry.json, using a mtime-based cache to avoid redundant reads."""
        try:
            mtime = os.path.getmtime(self._path)
            if mtime != self._mtime:
                with open(self._path) as f:
                    self._cache = json.load(f)
                self._mtime = mtime
        except Exception as e:
            logger.error(f"Failed to load registry.json: {e}")
            if not self._cache:
                self._cache = {"providers": {}}
        return self._cache

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        """Get rules for a specific provider."""
        return self._load().get("providers", {}).get(provider_id, {})

    def get_all_providers(self) -> dict[str, dict[str, Any]]:
        """Get all providers."""
        return self._load().get("providers", {})

    def resolve_path(self, path_str: str) -> str:
        """Resolve placeholders in path strings."""
        if path_str.startswith("~"):
            path_str = os.path.expanduser(path_str)

        if "{{CONFIG_DIR:" in path_str:
            import re

            match = re.search(r"{{CONFIG_DIR:([^}]+)}}", path_str)
            if match:
                app_name = match.group(1)
                path_str = path_str.replace(match.group(0), str(get_platform_config_dir(app_name)))

        if "{{DATA_DIR:" in path_str:
            import re

            match = re.search(r"{{DATA_DIR:([^}]+)}}", path_str)
            if match:
                app_name = match.group(1)
                path_str = path_str.replace(match.group(0), str(get_platform_data_dir(app_name)))

        return path_str


registry = Registry()
