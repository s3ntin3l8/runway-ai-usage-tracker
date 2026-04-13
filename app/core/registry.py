import json
import logging
import os
from typing import Any

from app.core.config import get_platform_config_dir, get_platform_data_dir

logger = logging.getLogger(__name__)


class Registry:
    """
    Centralized registry for AI provider extraction rules.
    Loads from app/core/registry.json.
    """

    def __init__(self):
        self._registry = self._load()

    def _load(self) -> dict[str, Any]:
        """Load registry.json from disk."""
        path = os.path.join(os.path.dirname(__file__), "registry.json")
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load registry.json: {e}")
            return {"providers": {}}

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        """Get rules for a specific provider."""
        return self._registry.get("providers", {}).get(provider_id, {})

    def get_all_providers(self) -> dict[str, dict[str, Any]]:
        """Get all providers."""
        return self._registry.get("providers", {})

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
