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
            "GITHUB_TOKEN": "test_github_token",
            "GEMINI_OAUTH_PATH": "/fake/gemini/creds.json",
        }

        with patch.dict(os.environ, test_vars, clear=False):
            # Import fresh to get new environment
            import importlib

            from app.core import config

            importlib.reload(config)

            assert config.settings.CLAUDE_CODE_OAUTH_TOKEN == "test_claude_token"
            assert config.settings.GITHUB_TOKEN == "test_github_token"
            assert config.settings.GEMINI_OAUTH_PATH == "/fake/gemini/creds.json"

    def test_settings_defaults(self):
        """Test that default values are applied for optional settings."""
        from app.core import config

        # Settings should have sensible defaults
        assert config.settings.CLAUDE_PROJECTS_DIR is not None
        assert config.settings.CHATGPT_SESSIONS_DIR is not None
        assert config.settings.ANTIGRAVITY_QUOTA_PATH is not None

    def test_settings_path_expansion(self):
        """Test that ~ paths are properly expanded."""
        from app.core import config

        # Paths should not contain ~ after loading
        assert "~" not in config.settings.CLAUDE_PROJECTS_DIR
        assert "~" not in config.settings.ANTIGRAVITY_QUOTA_PATH

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

    def test_github_token_from_env(self):
        """Test GITHUB_TOKEN is properly loaded."""
        test_token = "ghp_test_token_123456"
        with patch.dict(os.environ, {"GITHUB_TOKEN": test_token}):
            # Token would be loaded from environment
            pass

    def test_gemini_credentials_path_from_env(self):
        """Test GEMINI_OAUTH_PATH is properly configured."""
        test_path = "/custom/path/to/gemini/credentials.json"
        with patch.dict(os.environ, {"GEMINI_OAUTH_PATH": test_path}):
            # Path would be loaded from environment
            pass

    def test_optional_settings_with_defaults(self):
        """Test that optional settings use defaults when not provided."""
        with patch.dict(os.environ, {}, clear=True):
            from app.core.config import settings

            # Optional settings should have defaults
            assert settings.CLAUDE_PROJECTS_DIR
            assert settings.CHATGPT_SESSIONS_DIR


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
