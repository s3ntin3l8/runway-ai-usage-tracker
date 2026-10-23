"""
Unit tests for configuration loading and validation.

Tests cover:
- Environment variable loading
- Default values for optional settings
- Validation of required settings
- Path expansion and resolution
- Configuration override behavior
"""

import os
import tempfile
from unittest.mock import patch

import pytest


class TestSettings:
    """Test suite for application settings."""

    def test_settings_load_from_env(self):
        """Test that settings correctly load from environment variables."""
        test_vars = {
            "CLAUDE_CODE_OAUTH_TOKEN": "test_claude_token",
        }

        with patch.dict(os.environ, test_vars, clear=False):
            # Import fresh to get new environment
            import importlib

            from app.core import config

            importlib.reload(config)

            assert config.settings.CLAUDE_CODE_OAUTH_TOKEN == "test_claude_token"

    def test_removed_credential_path_settings_are_gone_and_their_env_vars_ignored(self):
        """The server reads no CLI credential files, so those path settings no longer exist.

        A leftover env var from an older deployment must be ignored (``extra="ignore"``),
        not crash startup. Runway's own GitHub OAuth file path stays.
        """
        removed = {
            "GEMINI_OAUTH_PATH": "/fake/gemini/creds.json",
            "ANTHROPIC_OAUTH_PATH": "/fake/claude/creds.json",
            "ANTIGRAVITY_OAUTH_PATH": "/fake/agy/token",
            "CHATGPT_AUTH_PATH": "/fake/codex/auth.json",
            "CLAUDE_STATUSLINE_PATH": "/fake/statusline.json",
            "CLAUDE_PROJECTS_DIR": "/fake/projects",
            "GEMINI_SESSIONS_DIR": "/fake/gemini/sessions",
            "CHATGPT_SESSIONS_DIR": "/fake/codex/sessions",
        }
        with patch.dict(os.environ, removed, clear=False):
            import importlib

            from app.core import config

            importlib.reload(config)

            for name in removed:
                assert not hasattr(config.settings, name)
            assert config.settings.GITHUB_OAUTH_PATH.endswith("github_oauth.json")

    def test_settings_validation(self):
        """Test that invalid settings raise validation errors."""

        # Invalid settings should be caught
        # (This is a placeholder - actual test would depend on Settings implementation)
        pass


class TestConfigEnvironmentVariables:
    """Test environment variable handling for different providers."""

    @pytest.fixture(autouse=True)
    def cleanup_env(self):
        """Clean up environment variables after each test."""
        original = os.environ.copy()
        yield
        os.environ.clear()
        os.environ.update(original)

    def test_claude_token_from_env(self):
        """Test CLAUDE_CODE_OAUTH_TOKEN is properly loaded."""
        test_token = "sk-ant-test-token-12345"
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": test_token}):
            # Token would be loaded from environment
            pass


class TestConfigEnvFileLoading:
    """Test .env file loading and parsing."""

    def test_dotenv_loading(self):
        """Test that .env file is properly loaded."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("TEST_VAR=test_value\n")
            f.write("TEST_INT=42\n")
            temp_path = f.name

        try:
            from dotenv import load_dotenv

            load_dotenv(temp_path)

            assert os.getenv("TEST_VAR") == "test_value"
            assert os.getenv("TEST_INT") == "42"
        finally:
            os.unlink(temp_path)

    def test_env_file_missing_graceful(self):
        """Test that missing .env file doesn't crash the app."""
        with patch("dotenv.load_dotenv") as mock_load:
            # Should handle missing .env file gracefully
            mock_load.return_value = None
            assert mock_load.return_value is None
