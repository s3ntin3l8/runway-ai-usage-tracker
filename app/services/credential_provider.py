import os
import json
import logging
import platform
from typing import Optional, Dict, List, Any
from app.core.config import settings
from app.core.registry import registry
from app.core.keychain import get_keychain_secret

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None


class CredentialProvider:
    """
    Centralized service for discovering credentials from various sources.
    Uses app/core/registry.json as the single source of truth via Registry service.
    """

    @staticmethod
    def _get_nested(data: Any, key_path: List[str]) -> Any:
        """Get nested value from dict using list of keys."""
        if not data or not isinstance(data, dict):
            return None

        current = data
        for k in key_path:
            if isinstance(current, dict):
                current = current.get(k)
            else:
                return None
        return current

    @staticmethod
    def get_credentials(provider_id: str) -> Dict[str, str]:
        """Generic extraction based on registry rules for a provider."""
        if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
            return {}

        results = {}
        provider_config = registry.get_provider(provider_id)
        rules = provider_config.get("rules", [])

        for rule in rules:
            rule_type = rule.get("type")
            mapping = rule.get("mapping", {})

            # 1. Environment Variables
            if rule_type == "env":
                target_key = mapping.get("value", "token")
                if target_key not in results:
                    val = os.getenv(rule.get("variable"))
                    if val:
                        results[target_key] = val

            # 2. Local Files (JSON/YAML)
            elif rule_type == "file":
                for path_str in rule.get("paths", []):
                    path = registry.resolve_path(path_str)
                    if os.path.exists(path):
                        try:
                            fmt = rule.get("format", "json")
                            with open(path, "r") as f:
                                if fmt == "yaml" and yaml:
                                    data = yaml.safe_load(f)
                                else:
                                    data = json.load(f)

                            for key_path_str, target in mapping.items():
                                if target not in results:
                                    # Strategy: Try to find the value by traversing the path.
                                    # We handle keys that might contain dots (like "github.com") 
                                    # by checking if the prefix is a valid key.
                                    val = CredentialProvider._resolve_mapping_value(data, key_path_str)
                                    if val:
                                        results[target] = val
                        except Exception as e:
                            logger.debug(f"Error reading file {path}: {e}")

            # 3. macOS Keychain
            elif rule_type == "keychain" and platform.system() == "Darwin":
                try:
                    needs_extraction = any(t not in results for t in mapping.values())
                    if not needs_extraction and mapping:
                        continue

                    # Use centralized keychain access with caching
                    service = rule.get("service")
                    raw = get_keychain_secret(service)
                    
                    if raw:
                        if rule.get("format") == "json":
                            data = json.loads(raw)
                            for key_path_str, target in mapping.items():
                                if target not in results:
                                    val = CredentialProvider._resolve_mapping_value(data, key_path_str)
                                    if val:
                                        results[target] = val
                        else:
                            target = mapping.get("value", "token")
                            if target not in results:
                                results[target] = raw
                except Exception:
                    pass

        return results

    @staticmethod
    def _resolve_mapping_value(data: Any, key_path_str: str) -> Any:
        """Helper to resolve a dot-notated key path from a data dict, handling keys with dots."""
        if not data or not isinstance(data, dict):
            return None
            
        # Try full key first
        if key_path_str in data:
            return data[key_path_str]
            
        # Split and try to find the longest prefix that is a key
        parts = key_path_str.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in data:
                val = data[prefix]
                if i == len(parts):
                    return val
                return CredentialProvider._resolve_mapping_value(val, ".".join(parts[i:]))
                
        # Default fallback to standard nested
        return CredentialProvider._get_nested(data, parts)

    @staticmethod
    def get_github_token() -> str:
        """Get GitHub token using registry rules."""
        creds = CredentialProvider.get_credentials("github")
        return creds.get("api_key", "")

    @staticmethod
    def get_gemini_credentials_path() -> Optional[str]:
        """Search for Gemini credentials file using registry rules."""
        if not settings.LOCAL_CREDENTIAL_SCRAPING_ENABLED:
            return None

        provider_config = registry.get_provider("gemini")
        for rule in provider_config.get("rules", []):
            if rule.get("type") == "file":
                for path_str in rule.get("paths", []):
                    path = registry.resolve_path(path_str)
                    if os.path.exists(path):
                        return path
        return None

    @staticmethod
    def get_chatgpt_data() -> Dict[str, str]:
        """Get full ChatGPT OAuth data using registry rules."""
        creds = CredentialProvider.get_credentials("chatgpt")
        # Ensure compatibility with existing keys
        if "oauth_token" in creds:
            creds["access_token"] = creds["oauth_token"]
        return creds

    @staticmethod
    def get_chatgpt_token() -> str:
        return CredentialProvider.get_chatgpt_data().get("access_token", "")

    _claude_token_cache: Optional[str] = None

    @classmethod
    def get_claude_token(cls) -> str:
        """Get Claude OAuth token using registry rules."""
        if cls._claude_token_cache:
            return cls._claude_token_cache

        creds = cls.get_credentials("anthropic")
        token = creds.get("oauth_token", "")
        if token:
            cls._claude_token_cache = token
        return token

    @classmethod
    def update_claude_token(cls, token: str) -> None:
        """Update the cached Claude token (called after a successful refresh)."""
        cls._claude_token_cache = token


credential_provider = CredentialProvider()
