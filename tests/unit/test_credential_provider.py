import json
import os
from unittest.mock import MagicMock, mock_open, patch

import pytest
import yaml

from app.services.credential_provider import CredentialProvider


@pytest.mark.asyncio
async def test_credential_provider_reads_the_selected_source_bundle(monkeypatch):
    from app.services import token_cache as token_cache_module
    from app.services.token_cache import TokenCache

    cache = TokenCache()
    monkeypatch.setattr(token_cache_module, "token_cache", cache)
    await cache.store(
        "anthropic",
        {
            "oauth_token": "source-oauth",  # pragma: allowlist secret — fake credential
            "session_cookie": "source-cookie",  # pragma: allowlist secret — fake credential
        },
        account_id="alice@example.com",
        source_id="sidecar:host-a",
        source_metadata={
            "source_type": "file",
            "sidecar_id": "host-a",
            "credential_origin": "path:/auth.json",
        },
    )

    async with cache.using_source("anthropic", "alice@example.com", "sidecar:host-a"):
        credentials = CredentialProvider.get_credentials(
            "anthropic", account_id="alice@example.com"
        )
        assert credentials["api_key"] == "source-oauth"  # pragma: allowlist secret
        assert credentials["access_token"] == "source-oauth"  # pragma: allowlist secret
        assert credentials.sources["oauth_token"] == "file"
        assert (
            CredentialProvider.get_provider_api_key("anthropic", account_id="alice@example.com")
            == "source-oauth"  # pragma: allowlist secret
        )
        assert (
            CredentialProvider.get_provider_session_cookie(
                "anthropic", account_id="alice@example.com"
            )
            == "source-cookie"
        )


@pytest.mark.asyncio
async def test_selected_but_missing_source_does_not_fall_back(monkeypatch):
    from app.services import token_cache as token_cache_module
    from app.services.token_cache import TokenCache

    cache = TokenCache()
    monkeypatch.setattr(token_cache_module, "token_cache", cache)
    async with cache.using_source("anthropic", "alice@example.com", "expired-source"):
        assert CredentialProvider.get_credentials("anthropic", account_id="alice@example.com") == {}
        assert (
            CredentialProvider.get_provider_api_key("anthropic", account_id="alice@example.com")
            is None
        )
        assert (
            CredentialProvider.get_provider_session_cookie(
                "anthropic", account_id="alice@example.com"
            )
            is None
        )


@pytest.mark.asyncio
async def test_active_source_for_other_account_does_not_block_legacy_discovery(monkeypatch):
    from app.services import token_cache as token_cache_module
    from app.services.token_cache import TokenCache

    cache = TokenCache()
    monkeypatch.setattr(token_cache_module, "token_cache", cache)
    monkeypatch.setenv("RUNWAY_TEST_BOB_KEY", "bob-env-key")
    monkeypatch.setattr(
        "app.services.credential_provider.registry.get_provider",
        lambda _provider: {
            "rules": [
                {
                    "type": "env",
                    "variable": "RUNWAY_TEST_BOB_KEY",
                    "mapping": {"value": "api_key"},
                }
            ]
        },
    )
    async with cache.using_source("anthropic", "alice@example.com", "alice-source"):
        assert (
            CredentialProvider.get_credentials("anthropic", account_id="bob@example.com")["api_key"]
            == "bob-env-key"
        )
        assert (
            CredentialProvider.get_credentials("openrouter")["api_key"]
            == "bob-env-key"  # pragma: allowlist secret — fake environment credential
        )


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
        patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        patch("os.path.exists", side_effect=exists_side_effect),
        patch("builtins.open", mock_open(read_data=mock_yaml)),
        patch(
            "app.services.credential_provider.yaml",
            MagicMock(safe_load=yaml.safe_load),
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
        patch("os.path.exists", side_effect=exists_side_effect),
        patch("os.path.expanduser", side_effect=lambda p: p.replace("~", "/home/user")),
    ):
        path = CredentialProvider.get_gemini_credentials_path()
        assert path is not None
        assert ".gemini/oauth_creds.json" in str(path)


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
        patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
        patch("os.path.exists", side_effect=lambda p: ".credentials.json" in str(p)),
        patch(
            "app.services.credential_provider.open",
            mock_open(read_data=mock_data),
            create=True,
        ),
    ):
        # Clear cache for test
        CredentialProvider._claude_token_cache = None
        token = CredentialProvider.get_claude_token()
        assert token == "claude_file_token"


def test_claude_oauth_creds_file_is_discovered():
    """Read Claude CLI credentials from its documented config-directory file."""
    mock_data = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "claude_oauth_creds_token",
                "refreshToken": "claude_oauth_creds_refresh",
            },
            "oauthAccount": {
                "emailAddress": "claude@example.com",
                "email": "Claude CLI",
            },
        }
    )

    with (
        patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
        patch(
            "os.path.exists",
            side_effect=lambda path: str(path).endswith("/claude/oauth_creds.json"),
        ),
        patch(
            "app.services.credential_provider.open",
            mock_open(read_data=mock_data),
            create=True,
        ),
    ):
        credentials = CredentialProvider.get_credentials("anthropic")

    assert credentials["oauth_token"] == "claude_oauth_creds_token"
    assert credentials["refresh_token"] == "claude_oauth_creds_refresh"
    assert credentials["account_id"] == "claude@example.com"
    assert credentials["account_label"] == "Claude CLI"


def test_mapping_value_pipe_syntax_falls_back_and_prefers_first_value():
    from app.services.credential_provider import CredentialProvider

    account_id_path = "oauthAccount.emailAddress|oauthAccount.email"
    assert (
        CredentialProvider._resolve_mapping_value(
            {"oauthAccount": {"email": "fallback@example.com"}}, account_id_path
        )
        == "fallback@example.com"
    )
    assert (
        CredentialProvider._resolve_mapping_value(
            {
                "oauthAccount": {
                    "emailAddress": "preferred@example.com",
                    "email": "fallback@example.com",
                }
            },
            account_id_path,
        )
        == "preferred@example.com"
    )


def test_db_read_failures_are_swallowed():
    """A DB error while reading ProviderConfig must degrade gracefully, not raise.

    Exercises the defensive ``except Exception`` branches in get_credentials,
    get_provider_api_key, and get_provider_session_cookie by making the local
    ``Session(...)`` construction blow up.
    """
    with patch("sqlmodel.Session", side_effect=RuntimeError("db down")):
        # get_credentials still returns a CredentialMap without raising
        # (the DB override block is skipped; env/file sources, if any, still apply)
        creds = CredentialProvider.get_credentials("provider-with-no-env-or-file")
        assert isinstance(creds, dict)

        # The single-value getters fall back to None when the DB read fails
        assert CredentialProvider.get_provider_api_key("provider-with-no-env-or-file") is None
        assert (
            CredentialProvider.get_provider_session_cookie("provider-with-no-env-or-file") is None
        )


def test_expand_rule_paths_glob_freshest_first(tmp_path):
    """Glob matches sort by mtime descending — the file loop is first-match-wins
    per target key, so the freshest credential must come first. (The sidecar's
    loop overwrites per file and sorts ascending; both end with the freshest.)"""
    from app.services.credential_provider import _expand_rule_paths

    older = tmp_path / "kimi-code-env-aaa.json"
    older.write_text("{}")
    newer = tmp_path / "kimi-code-env-bbb.json"
    newer.write_text("{}")
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    matches = _expand_rule_paths([str(tmp_path / "kimi-code-env-*.json")])
    assert matches == [str(newer), str(older)]


def test_expand_rule_paths_valid_token_beats_newer_expired(tmp_path):
    """A stale-but-recently-touched file must not beat a valid token:
    valid files sort first (first-match-wins), expired ones after."""
    from app.services.credential_provider import _expand_rule_paths

    valid = tmp_path / "kimi-code-env-valid.json"
    valid.write_text(json.dumps({"access_token": "good", "expires_at": 9999999999}))
    stale = tmp_path / "kimi-code-env-stale.json"
    stale.write_text(json.dumps({"access_token": "bad", "expires_at": 1000}))
    os.utime(valid, (1000, 1000))  # older mtime …
    os.utime(stale, (2000, 2000))  # … but stale is the recently-touched one

    matches = _expand_rule_paths([str(tmp_path / "kimi-code-env-*.json")])
    assert matches == [str(valid), str(stale)]


def test_expand_rule_paths_plain_exact_match(tmp_path):
    """Non-glob entries keep exact-match behavior; missing files are skipped."""
    from app.services.credential_provider import _expand_rule_paths

    existing = tmp_path / "kimi-code.json"
    existing.write_text("{}")
    assert _expand_rule_paths([str(existing)]) == [str(existing)]
    assert _expand_rule_paths([str(tmp_path / "missing.json")]) == []


# The three sibling providers whose opencode auth.json rule was added to
# registry.json by #351: (provider_id, env var that must win, auth.json key).
_OPENCODE_AUTH_JSON_PROVIDERS = [
    ("openrouter", "OPENROUTER_API_KEY", "openrouter"),
    ("minimax", "MINIMAX_API_KEY", "minimax-coding-plan"),
    ("kimi_coding", "KIMI_CODE_API_KEY", "kimi-code-plan-global"),
]


@pytest.mark.parametrize(("provider_id", "env_var", "service_key"), _OPENCODE_AUTH_JSON_PROVIDERS)
def test_opencode_auth_json_file_rule_extracts_nested_api_key(
    provider_id, env_var, service_key, tmp_path, monkeypatch
):
    """The opencode auth.json file rule maps `<service>.key` -> api_key (#351).

    ``_resolve_mapping_value`` must descend into auth.json's nested
    ``{"<service>": {"key": ...}}`` shape the way it already does for
    ``ollama-cloud.key`` — the server-side half of the registry parity fix.
    """
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({service_key: {"key": "sk-live-from-auth"}}))

    monkeypatch.setattr(
        "app.services.credential_provider._expand_rule_paths", lambda _paths: [str(auth_path)]
    )
    monkeypatch.setenv(env_var, "")

    creds = CredentialProvider.get_credentials(provider_id)

    assert creds["api_key"] == "sk-live-from-auth"  # pragma: allowlist secret
    assert creds.sources["api_key"] == "server"  # pragma: allowlist secret


@pytest.mark.parametrize(("provider_id", "env_var", "service_key"), _OPENCODE_AUTH_JSON_PROVIDERS)
def test_opencode_auth_json_env_rule_beats_file_rule(
    provider_id, env_var, service_key, monkeypatch, tmp_path
):
    """Rule order mirrors the sidecar: a set env var wins over the file lookup.

    Parametrized across all three siblings so the env-before-file placement
    that keeps ``get_credentials`` first-wins is pinned for each of them.
    """
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({service_key: {"key": "sk-from-file"}}))

    monkeypatch.setattr(
        "app.services.credential_provider._expand_rule_paths", lambda _paths: [str(auth_path)]
    )
    monkeypatch.setenv(env_var, "sk-from-env")

    creds = CredentialProvider.get_credentials(provider_id)

    assert creds["api_key"] == "sk-from-env"  # pragma: allowlist secret
