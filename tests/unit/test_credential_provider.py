import json
import os
from unittest.mock import MagicMock, mock_open, patch

import yaml

from app.services.credential_provider import CredentialProvider


def test_github_token_env():
    """Test discovering GitHub token from environment."""
    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": "env_token"}),
        patch("os.path.exists", return_value=False),
    ):
        token = CredentialProvider.get_github_token()
        assert token == "env_token"


def test_github_token_runway_json():
    """Test discovering GitHub token from Runway's oauth.json."""
    mock_data = json.dumps({"access_token": "runway_token"})
    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        patch("os.path.exists", side_effect=lambda p: "github_oauth.json" in str(p)),
        patch("builtins.open", mock_open(read_data=mock_data)),
    ):
        token = CredentialProvider.get_github_token()
        assert token == "runway_token"


def test_github_token_gh_cli():
    """Test discovering GitHub token from gh CLI's hosts.yml."""
    mock_yaml = "github.com:\n  oauth_token: gho_cli_token\n  user: test"

    # Need to patch os.path.exists for both Runway path (return False) and gh path (return True)
    def exists_side_effect(path):
        if "hosts.yml" in str(path):
            return True
        return False

    with (
        patch(
            "app.services.credential_provider.is_local_credential_scraping_enabled",
            return_value=True,
        ),
        patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        patch("os.path.exists", side_effect=exists_side_effect),
        patch("builtins.open", mock_open(read_data=mock_yaml)),
        patch(
            "app.services.credential_provider.yaml",
            MagicMock(safe_load=lambda f: yaml.safe_load(f)),
        ),
    ):
        token = CredentialProvider.get_github_token()
        assert token == "gho_cli_token"


def test_gemini_path_discovery():
    """Test discovering Gemini credentials path."""

    def exists_side_effect(path):
        if ".gemini/oauth_creds.json" in str(path):
            return True
        return False

    with (
        patch(
            "app.services.credential_provider.is_local_credential_scraping_enabled",
            return_value=True,
        ),
        patch("os.path.exists", side_effect=exists_side_effect),
        patch("os.path.expanduser", side_effect=lambda p: p.replace("~", "/home/user")),
    ):
        path = CredentialProvider.get_gemini_credentials_path()
        assert path is not None
        assert ".gemini/oauth_creds.json" in str(path)


def test_disabled_scraping():
    """Test that discovery returns empty/None if scraping is disabled."""
    with (
        patch(
            "app.services.credential_provider.is_local_credential_scraping_enabled",
            return_value=False,
        ),
        patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        patch("os.path.exists", return_value=False),
    ):
        assert CredentialProvider.get_github_token() == ""
        assert CredentialProvider.get_gemini_credentials_path() is None


def test_claude_token_env():
    """Test discovering Claude token from environment."""
    with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "claude_env_token"}):
        # Clear cache for test
        CredentialProvider._claude_token_cache = None
        token = CredentialProvider.get_claude_token()
        assert token == "claude_env_token"


def test_claude_token_file():
    """Test discovering Claude token from .credentials.json."""
    mock_data = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "claude_file_token",
                "refreshToken": "claude_refresh_token",
            }
        }
    )
    with (
        patch(
            "app.services.credential_provider.is_local_credential_scraping_enabled",
            return_value=True,
        ),
        patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
        patch("os.path.exists", side_effect=lambda p: ".credentials.json" in str(p)),
        patch("builtins.open", mock_open(read_data=mock_data)),
    ):
        # Clear cache for test
        CredentialProvider._claude_token_cache = None
        token = CredentialProvider.get_claude_token()
        assert token == "claude_file_token"
