import json
import os
from unittest.mock import mock_open, patch

import pytest

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
        token = CredentialProvider.get_github_data().get("api_key", "")
        assert token == "env_token"


def test_github_token_runway_json():
    """Test discovering GitHub token from Runway's oauth.json."""
    mock_data = json.dumps({"access_token": "runway_token"})
    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": ""}),
        patch("os.path.exists", side_effect=lambda p: "github_oauth.json" in str(p)),
        patch("builtins.open", mock_open(read_data=mock_data)),
    ):
        token = CredentialProvider.get_github_data().get("api_key", "")
        assert token == "runway_token"


def test_github_token_gh_cli_file_is_not_read_by_the_server(tmp_path, monkeypatch):
    """gh's hosts.yml is a sidecar concern: the server never evaluates that file rule."""
    hosts = tmp_path / ".config" / "gh" / "hosts.yml"
    hosts.parent.mkdir(parents=True)
    hosts.write_text("github.com:\n  oauth_token: gho_cli_token\n  user: test")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway-config"))
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("GH_TOKEN", "")

    creds = CredentialProvider.get_github_data()

    assert "api_key" not in creds
    assert not any(v == "gho_cli_token" for v in creds.values())


def test_claude_token_env():
    """Test discovering Claude token from environment."""
    with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "claude_env_token"}):
        # Clear cache for test
        CredentialProvider._claude_token_cache = None
        token = CredentialProvider.get_claude_token()
        assert token == "claude_env_token"


def test_claude_cli_credential_files_are_not_read_by_the_server(tmp_path, monkeypatch):
    """~/.claude/.credentials.json and the claude config dir's oauth_creds.json are
    sidecar-only: the server's own host never contributes a Claude login."""
    payload = json.dumps(
        {
            "claudeAiOauth": {"accessToken": "claude_file_token", "refreshToken": "r"},
            "oauthAccount": {"emailAddress": "claude@example.com"},
        }
    )
    for rel in (".claude/.credentials.json", ".config/claude/oauth_creds.json"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(payload)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway-config"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    CredentialProvider._claude_token_cache = None

    assert CredentialProvider.get_claude_token() == ""
    credentials = CredentialProvider.get_credentials("anthropic")
    assert "oauth_token" not in credentials
    assert "account_id" not in credentials


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
def test_opencode_auth_json_file_rule_yields_nothing_on_the_server(
    provider_id, env_var, service_key, tmp_path, monkeypatch
):
    """The opencode auth.json rule (#351) is sidecar-only now: even a valid file in the
    server host's home contributes no credential and no ``file`` origin."""
    auth_path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({service_key: {"key": "sk-live-from-auth"}}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway-config"))
    monkeypatch.setenv(env_var, "")

    creds = CredentialProvider.get_credentials(provider_id)

    assert "api_key" not in creds
    assert not [
        o
        for o in CredentialProvider.server_credential_origins(provider_id)
        if o["source_type"] == "file"
    ]


@pytest.mark.parametrize(("provider_id", "env_var", "service_key"), _OPENCODE_AUTH_JSON_PROVIDERS)
def test_opencode_auth_json_env_rule_still_works(
    provider_id, env_var, service_key, monkeypatch, tmp_path
):
    """The env var is still a server credential, with or without an auth.json beside it."""
    auth_path = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(json.dumps({service_key: {"key": "sk-from-file"}}))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "runway-config"))
    monkeypatch.setenv(env_var, "sk-from-env")

    creds = CredentialProvider.get_credentials(provider_id)

    assert creds["api_key"] == "sk-from-env"  # pragma: allowlist secret
    # pragma: allowlist nextline secret
    assert creds.sources["api_key"] == "server"


def test_a_file_rule_outside_runways_config_dir_is_never_evaluated(monkeypatch):
    """``_server_may_read`` gates the RAW paths, before any ``~`` expansion or glob."""
    from app.services import credential_provider as cp

    assert cp._server_may_read({"paths": ["{{CONFIG_DIR:runway}}/github_oauth.json"]})
    assert not cp._server_may_read({"paths": ["~/.gemini/oauth_creds.json"]})
    assert not cp._server_may_read({"paths": ["{{CONFIG_DIR:gemini}}/oauth_creds.json"]})
    # One foreign path poisons the rule: the whole rule stays off the server.
    assert not cp._server_may_read(
        {"paths": ["{{CONFIG_DIR:runway}}/a.json", "~/.config/gh/hosts.yml"]}
    )
    assert not cp._server_may_read({"paths": []})

    def boom(_paths):
        raise AssertionError("_expand_rule_paths must not run for a sidecar-only rule")

    monkeypatch.setattr(cp, "_expand_rule_paths", boom)
    monkeypatch.setattr(
        cp.registry,
        "get_provider",
        lambda _pid: {
            "rules": [
                {
                    "type": "file",
                    "paths": ["~/.gemini/oauth_creds.json"],
                    "mapping": {"access_token": "oauth_token"},
                }
            ]
        },
    )
    assert CredentialProvider.get_credentials("gemini") == {}
    assert CredentialProvider.server_credential_origins("gemini") == []


def test_server_credential_origins_classify_a_credential_without_returning_it(monkeypatch):
    """The scan derives expiry and refreshability from the values so the inventory can
    classify a server credential; the values themselves must never come back."""
    import base64
    import time

    from app.services.credential_provider import CredentialProvider

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    exp = time.time() - 600
    token = f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"
    monkeypatch.setattr(
        "app.services.credential_provider.registry.get_provider",
        lambda _pid: {
            "rules": [
                {"type": "env", "variable": "SOME_OAUTH_TOKEN", "mapping": {"value": "oauth_token"}}
            ]
        },
    )
    monkeypatch.setenv("SOME_OAUTH_TOKEN", token)

    (origin,) = CredentialProvider.server_credential_origins("anything")

    assert origin["exp"] == pytest.approx(exp, abs=1)
    assert origin["rollable"] is False
    assert token not in json.dumps(origin)


def test_resolve_mapping_value_handles_colon_and_dot_keys_like_the_sidecar():
    key = (
        "github.com:Iv1.b507a08c87ecfe98.oauth_token|github.com:Iv23ctfURkiMfJ4xr5mv.oauth_token"
        "|github.com.oauth_token"
    )
    data = {"github.com:Iv23ctfURkiMfJ4xr5mv": {"user": "me", "oauth_token": "ghu_abc"}}

    assert CredentialProvider._resolve_mapping_value(data, key) == "ghu_abc"
    assert (
        CredentialProvider._resolve_mapping_value({"github.com": {"oauth_token": "x"}}, key) == "x"
    )
