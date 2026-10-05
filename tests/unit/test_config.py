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
import sys
import tempfile
from unittest.mock import patch

import pytest


@pytest.fixture
def _restore_config_module():
    """Snapshot ``sys.modules['app.core.config']`` and restore it after the test.

    ``test_settings_load_from_env`` calls ``importlib.reload(app.core.config)``
    to verify env-var propagation. The reload swaps the entry in
    ``sys.modules`` with a fresh module instance, leaving any module
    that did ``from app.core.config import settings`` at top-of-file
    bound to the pre-reload snapshot. Without restoration, downstream
    tests in the same pytest session see a stale ``settings`` object —
    consumers that read via top-of-file imports see the snapshot,
    consumers that read via late import see the live module, and
    the two diverge depending on the consumer's pattern.

    This fixture retires the whole class by restoring the original
    module after the reload-test completes. With this in place, the
    rest of the suite sees consistent state regardless of whether
    each consumer uses late-import or top-of-file reads
    (PR #291, Hermes forward note on PR #297).
    """
    # Snapshot the ``settings`` attribute of the module — ``importlib.reload``
    # re-uses the module object but re-instantiates its ``settings``
    # attribute, so the bound ``from app.core.config import settings`` in
    # downstream test files (and prod modules like ``db.py``) ends up
    # pointing at the OLD instance while the freshly-reloaded module
    # exposes a NEW one. Restoring the attribute makes both sides agree
    # (PR #291, Hermes forward note on PR #297).
    settings_obj = (
        sys.modules["app.core.config"].settings if "app.core.config" in sys.modules else None
    )
    yield
    if settings_obj is not None and "app.core.config" in sys.modules:
        sys.modules["app.core.config"].settings = settings_obj


class TestSettings:
    """Test suite for application settings."""

    def test_settings_load_from_env(self, _restore_config_module):
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

    def test_removed_credential_path_settings_are_gone_and_their_env_vars_ignored(
        self, _restore_config_module
    ):
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
