"""Unit tests for sidecar credential manifest generation and resolved-hints caching."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

from tests.unit._sidecar_helpers import (
    REPO_ROOT,
    _StubCache,
    isolate_host_credentials,  # noqa: F401 — fixture
)

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
import sidecar


class TestCredentialOriginForProvider:
    """The ``credential_origin_for_provider`` helper is the seam between
    the silent-listener block guard (looks up the operator's hint) and
    the manifest ``blocked_origins`` entry (reports the unresolved
    origin). PR #290 round-2 review (Hermes thread Vha6): a #289
    widening changes the helper, not the two call sites — so the
    guard and the manifest can't drift apart silently.

    Phase 1 returns ``f"provider:{provider_id}"``. The shape is
    pinned here so a future widening is intentional, not accidental.
    """

    def test_returns_phase1_coarse_descriptor(self):
        from scripts.sidecar import credential_origin_for_provider

        assert credential_origin_for_provider("anthropic") == "provider:anthropic"
        assert credential_origin_for_provider("chatgpt") == "provider:chatgpt"

    def test_descriptor_is_stable_across_call_sites(self):
        """Guard-lookup and manifest-report must use the same string
        for the same provider_id — that's the whole point of the
        seam (PR #290 round-2 Hermes thread Vha6)."""
        # Use the top-level ``sidecar`` binding (resolved via
        # ``sys.path.insert`` at module top) instead of a local
        # ``import scripts.sidecar as sc`` — CodeQL flags the
        # dual-style import when the file already has a top-level
        # ``import sidecar``.
        assert sidecar.credential_origin_for_provider("antigravity") == "provider:antigravity"
        # And the helper is importable / accessible at module scope.
        assert hasattr(sidecar, "credential_origin_for_provider")


class TestPostCredentialManifest:
    """The silent-listener manifest POST. PR #288 shipped the endpoint;
    PR #290 round-2 review added (a) the ``resolved``-field consumption
    callback so the loop closes on the same cycle, and (b) the gate on
    completed cycle so ``error_count > 0`` doesn't prune still-unresolved
    pending rows.
    """

    def _mock_response(self, payload: dict, status: int = 200) -> MagicMock:
        body = json.dumps(payload).encode("utf-8")
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(
            return_value=MagicMock(
                getcode=MagicMock(return_value=status), read=MagicMock(return_value=body)
            )
        )
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    def test_health_observations_include_only_token_types_and_expiry(self):
        card = {
            "remaining": "Token",
            "unit": "oauth",
            "metadata": {
                "provider_id": "antigravity",
                "credential_origin": "path:/agy/oauth.json",
                "oauth_token": "private-token-value",
                "refresh_token": "private-refresh-value",
                "expiry_date": str(1_700_000_000_000),
            },
        }

        observation = sidecar._credential_health_observations([card])[0]

        assert observation == {
            "provider_id": "antigravity",
            "credential_origin": "path:/agy/oauth.json",
            "token_types": ["oauth_token", "refresh_token"],
            "expires_at": 1_700_000_000,
        }
        assert "private-token-value" not in json.dumps(observation)

    def test_health_observations_union_token_types_for_one_origin(self):
        cards = [
            {
                "remaining": "Token",
                "unit": "oauth",
                "metadata": {
                    "provider_id": "antigravity",
                    "credential_origin": "path:/auth.json",
                    "oauth_token": "opaque-oauth",
                },
            },
            {
                "remaining": "Token",
                "unit": "api_key",
                "metadata": {
                    "provider_id": "antigravity",
                    "credential_origin": "path:/auth.json",
                    "api_key": "opaque-api-key",  # pragma: allowlist secret
                },
            },
        ]

        observations = sidecar._credential_health_observations(cards)

        assert len(observations) == 1
        assert observations[0]["token_types"] == ["api_key", "oauth_token"]

    def test_health_observation_skips_metadata_only_provider_origin(self):
        """An exec rule that carried no credential key (e.g. git user.email) is not a credential."""
        card = {
            "remaining": "Token",
            "unit": "oauth",
            "metadata": {
                "provider_id": "github",
                "credential_origin": "provider:github",
                "name": "me@example.com",
            },
        }

        assert sidecar._credential_health_observations([card]) == []

    def test_health_observation_keeps_provider_origin_with_a_token(self):
        card = {
            "remaining": "Token",
            "unit": "api_key",
            "metadata": {
                "provider_id": "github",
                "credential_origin": "provider:github",
                "api_key": "opaque",  # pragma: allowlist secret
            },
        }

        (observation,) = sidecar._credential_health_observations([card])

        assert observation["token_types"] == ["api_key"]

    def test_file_rule_spellings_of_one_file_yield_one_candidate(
        self, monkeypatch, tmp_path, make_symlink
    ):
        hosts = tmp_path / "hosts.yml"
        hosts.write_text("github.com:\n  oauth_token: gho_abc\n")
        alias = tmp_path / "alias"
        make_symlink(alias, tmp_path)
        monkeypatch.setattr(
            sidecar, "expand_file_rule_paths", lambda _paths: [hosts, alias / "hosts.yml"]
        )
        config = {
            "name": "GitHub Copilot",
            "rules": [
                {
                    "type": "file",
                    "paths": ["ignored"],
                    "format": "yaml",
                    "mapping": {"github.com.oauth_token": "api_key"},
                }
            ],
        }

        cards, _ = sidecar.GenericCollector.collect_provider("github", config)

        assert len([c for c in cards if c.get("remaining") == "Token"]) == 1

    def _copilot_config(self):
        (rule,) = [
            r
            for r in sidecar.__REGISTRY__["providers"]["github"]["rules"]
            if r["type"] == "file" and any("github-copilot" in p for p in r["paths"])
        ]
        return {"name": "GitHub Copilot", "rules": [rule]}

    def test_colon_and_dot_keys_and_the_app_id_chain_resolve(self):
        key = next(iter(self._copilot_config()["rules"][0]["mapping"]))
        for app_id in ("Iv1.b507a08c87ecfe98", "Iv23ctfURkiMfJ4xr5mv", "Ov23liV9UpD7Rnfnskm3"):
            data = {f"github.com:{app_id}": {"user": "me", "oauth_token": "ghu_abc"}}
            assert sidecar.GenericCollector.get_nested(data, key) == "ghu_abc"
        assert sidecar.GenericCollector.get_nested(
            {"github.com": {"oauth_token": "ghu_old"}}, key
        ) == ("ghu_old")

    def test_copilot_apps_json_yields_one_pending_token_without_account_id(
        self, monkeypatch, tmp_path
    ):
        apps = tmp_path / "apps.json"
        apps.write_text(
            json.dumps(
                {
                    "foo.ghe.com:Iv23ctfURkiMfJ4xr5mv": {"user": "ent", "oauth_token": "ghu_ent"},
                    "github.com:Iv23ctfURkiMfJ4xr5mv": {"user": "me", "oauth_token": "ghu_me"},
                }
            )
        )
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [apps])

        cards, _ = sidecar.GenericCollector.collect_provider("github", self._copilot_config())

        (card,) = [c for c in cards if c.get("remaining") == "Token"]
        assert card["metadata"]["api_key"] == "ghu_me"
        assert "ghu_ent" not in json.dumps(card)
        assert not card.get("account_id")
        assert card["metadata"]["identity_pending"] is True

    def test_the_same_token_in_apps_and_hosts_json_is_one_credential(self, monkeypatch, tmp_path):
        apps = tmp_path / "apps.json"
        apps.write_text(
            json.dumps({"github.com:Iv1.b507a08c87ecfe98": {"oauth_token": "ghu_same"}})
        )
        hosts = tmp_path / "hosts.json"
        hosts.write_text(json.dumps({"github.com": {"oauth_token": "ghu_same"}}))
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [apps, hosts])

        cards, _ = sidecar.GenericCollector.collect_provider("github", self._copilot_config())

        assert len([c for c in cards if c.get("remaining") == "Token"]) == 1

    def test_a_readable_file_without_credentials_is_logged_once(
        self, monkeypatch, tmp_path, caplog
    ):
        import logging

        monkeypatch.setattr(sidecar, "_KEYLESS_FILES_LOGGED", set())
        apps = tmp_path / "apps.json"
        apps.write_text(json.dumps({"github.com:Iv1.b507a08c87ecfe98": {"user": "me"}}))
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [apps])

        with caplog.at_level(logging.INFO):
            for _ in range(3):
                sidecar.GenericCollector.collect_provider("github", self._copilot_config())

        lines = [r.getMessage() for r in caplog.records if "no credential keys" in r.getMessage()]
        assert len(lines) == 1
        assert str(apps) in lines[0]

    def _copilot_cli_config(self):
        (rule,) = [
            r
            for r in sidecar.__REGISTRY__["providers"]["github"]["rules"]
            if r["type"] == "file" and r.get("format") == "jsonc"
        ]
        return {"name": "GitHub Copilot", "rules": [rule]}

    def _collect_cli_file(self, monkeypatch, tmp_path, text: str, *, bom: bool = False):
        path = tmp_path / "config.json"
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [path])
        cards, _ = sidecar.GenericCollector.collect_provider("github", self._copilot_cli_config())
        return [c for c in cards if c.get("remaining") == "Token"]

    CLI_AUTH_TOKENS = (
        "// User settings belong in settings.json.\n"
        "// This file is managed automatically.\n"
        "{\n"
        '  "lastLoggedInUser": {"host": "https://github.com", "login": "me"}, // who\n'
        '  "authTokens": {\n'
        '    "https://github.com:me": {"token": "gho_clitoken"}, /* current */\n'
        '    "https://corp.ghe.com:me": {"token": "gho_enterprise"}\n'
        "  }\n"
        "}\n"
    )

    def test_copilot_cli_config_with_comments_yields_the_github_com_token_only(
        self, monkeypatch, tmp_path
    ):
        (card,) = self._collect_cli_file(monkeypatch, tmp_path, self.CLI_AUTH_TOKENS)

        assert card["metadata"]["api_key"] == "gho_clitoken"  # pragma: allowlist secret
        assert "gho_enterprise" not in json.dumps(card)
        # The login in the key is never captured: the server verifies identity via /user.
        assert not card.get("account_id")
        assert card["metadata"]["identity_pending"] is True

    def test_copilot_cli_config_with_a_byte_order_mark_is_read(self, monkeypatch, tmp_path):
        (card,) = self._collect_cli_file(monkeypatch, tmp_path, self.CLI_AUTH_TOKENS, bom=True)

        assert card["metadata"]["api_key"] == "gho_clitoken"  # pragma: allowlist secret

    def test_copilot_cli_older_string_token_shape_is_read(self, monkeypatch, tmp_path):
        text = (
            '{"copilotTokens": {"https://github.com:me": "gho_old", "https://x.ghe.com:me": "no"}}'
        )

        (card,) = self._collect_cli_file(monkeypatch, tmp_path, text)

        assert card["metadata"]["api_key"] == "gho_old"  # pragma: allowlist secret

    def test_copilot_cli_config_without_a_token_yields_nothing_and_does_not_log(
        self, monkeypatch, tmp_path, caplog
    ):
        import logging

        monkeypatch.setattr(sidecar, "_KEYLESS_FILES_LOGGED", set())
        text = "// managed\n" + '{"trustedFolders": [], "authTokens": {}}'

        with caplog.at_level(logging.INFO):
            cards = self._collect_cli_file(monkeypatch, tmp_path, text)

        assert cards == []
        # Machines with a working keychain have exactly this file, so it must stay quiet.
        assert not [r for r in caplog.records if "no credential keys" in r.getMessage()]

    def test_the_editor_rule_catches_an_unknown_client_id_last(self, monkeypatch, tmp_path):
        apps = tmp_path / "apps.json"
        apps.write_text(
            json.dumps(
                {
                    "github.com:BrandNewClient": {"oauth_token": "ghu_new"},
                    "github.com:Iv23ctfURkiMfJ4xr5mv": {"oauth_token": "ghu_known"},
                }
            )
        )
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [apps])

        cards, _ = sidecar.GenericCollector.collect_provider("github", self._copilot_config())

        (card,) = [c for c in cards if c.get("remaining") == "Token"]
        assert card["metadata"]["api_key"] == "ghu_known"  # pragma: allowlist secret

    def test_strip_jsonc_leaves_string_contents_alone(self):
        text = '// c\n{"u": "https://github.com:me", /* x */ "q": "a \\" // b"}'

        assert json.loads(sidecar._strip_jsonc(text)) == {
            "u": "https://github.com:me",
            "q": 'a " // b',
        }

    def test_two_env_vars_holding_one_token_are_one_credential(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gho_same")
        monkeypatch.setenv("GH_TOKEN", "gho_same")
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [])
        config = {
            "name": "GitHub Copilot",
            "rules": [
                {"type": "env", "variable": "GITHUB_TOKEN", "mapping": {"value": "api_key"}},
                {"type": "env", "variable": "GH_TOKEN", "mapping": {"value": "api_key"}},
            ],
        }

        cards, _ = sidecar.GenericCollector.collect_provider("github", config)

        assert len([c for c in cards if c.get("remaining") == "Token"]) == 1

    def test_exec_rule_echoing_an_already_found_token_is_not_a_second_credential(
        self, monkeypatch, tmp_path
    ):
        hosts = tmp_path / "hosts.yml"
        hosts.write_text("github.com:\n  oauth_token: gho_abc\n")
        monkeypatch.setattr(sidecar, "expand_file_rule_paths", lambda _paths: [hosts])
        monkeypatch.setattr(
            sidecar.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="gho_abc\n", stderr=""),
        )
        config = {
            "name": "GitHub Copilot",
            "rules": [
                {
                    "type": "file",
                    "paths": ["ignored"],
                    "format": "yaml",
                    "mapping": {"github.com.oauth_token": "api_key"},
                },
                {
                    "type": "exec",
                    "command": ["gh", "auth", "token"],
                    "mapping": {"value": "api_key"},
                },
            ],
        }

        cards, _ = sidecar.GenericCollector.collect_provider("github", config)

        assert len([c for c in cards if c.get("remaining") == "Token"]) == 1

    def test_health_observation_accepts_epoch_zero_jwt_expiry(self):
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        epoch_payload = base64.urlsafe_b64encode(b'{"exp":0}').rstrip(b"=").decode()
        later_payload = base64.urlsafe_b64encode(b'{"exp":123}').rstrip(b"=").decode()
        metadata = {
            "provider_id": "chatgpt",
            "credential_origin": "path:/auth.json",
            "id_token": f"{header}.{epoch_payload}.sig",
            "oauth_token": f"{header}.{later_payload}.sig",
        }

        observation = sidecar._credential_health_observations(
            [{"remaining": "Token", "unit": "oauth", "metadata": metadata}]
        )[0]

        assert observation["expires_at"] == 0

    def test_health_observation_accepts_epoch_zero_expiry_date(self):
        card = {
            "remaining": "Token",
            "unit": "oauth",
            "metadata": {
                "provider_id": "antigravity",
                "credential_origin": "path:/agy/oauth.json",
                "oauth_token": "opaque-token",
                "expiry_date": 0,
            },
        }

        observation = sidecar._credential_health_observations([card])[0]

        assert observation["expires_at"] == 0

    def test_consumes_resolved_field_into_callback(self, monkeypatch):
        """The server's ``resolved`` map is delivered to ``on_resolved``
        so the local hint cache merges operator tags on the same cycle
        (PR #290 round-2 review, Hermes suggestion #9)."""
        captured: dict[str, dict[str, str]] = {}

        def _capture(resolved):
            captured.update(resolved)

        ctx = self._mock_response(
            {
                "status": "ok",
                "sidecar_id": "alpha-host",
                "entries_received": 1,
                "entries_pruned": 0,
                "resolved": {
                    "anthropic": {"provider:anthropic": "alice@example.com"},
                    "chatgpt": {
                        "provider:chatgpt": "alice@example.com",
                    },
                },
            }
        )

        with (
            patch("scripts.sidecar_pkg.tls.build_context", return_value=None),
            patch("scripts.sidecar_pkg.transport.urlopen", return_value=ctx),
        ):
            sidecar._post_credential_manifest(
                api_url="https://api.example.com",
                api_key="test-key",
                sidecar_id="alpha-host",
                entries=[{"provider_id": "anthropic", "credential_origin": "provider:anthropic"}],
                on_resolved=_capture,
            )

        assert captured == {
            "anthropic": {"provider:anthropic": "alice@example.com"},
            "chatgpt": {"provider:chatgpt": "alice@example.com"},
        }

    def test_skips_callback_when_resolved_is_empty(self):
        """An empty ``resolved`` map (operator hasn't tagged anything
        new) is a no-op — no callback invocation, no log noise."""
        callback = MagicMock()
        ctx = self._mock_response(
            {
                "status": "ok",
                "sidecar_id": "alpha-host",
                "entries_received": 1,
                "entries_pruned": 0,
                "resolved": {},
            }
        )

        with (
            patch("scripts.sidecar_pkg.tls.build_context", return_value=None),
            patch("scripts.sidecar_pkg.transport.urlopen", return_value=ctx),
        ):
            sidecar._post_credential_manifest(
                api_url="https://api.example.com",
                api_key="test-key",
                sidecar_id="alpha-host",
                entries=[],
                on_resolved=callback,
            )

        callback.assert_not_called()

    def test_skips_callback_on_non_200(self):
        """A non-200 response is treated like an outage — the callback
        is NOT invoked. The next cycle retries."""
        callback = MagicMock()
        ctx = self._mock_response({"error": "boom"}, status=500)

        with (
            patch("scripts.sidecar_pkg.tls.build_context", return_value=None),
            patch("scripts.sidecar_pkg.transport.urlopen", return_value=ctx),
        ):
            sidecar._post_credential_manifest(
                api_url="https://api.example.com",
                api_key="test-key",
                sidecar_id="alpha-host",
                entries=[],
                on_resolved=callback,
            )

        callback.assert_not_called()

    def test_skips_callback_when_resolved_field_missing(self):
        """Older server versions (pre-PR #288) won't carry ``resolved``.
        The callback is silently skipped — the next cycle retries."""
        callback = MagicMock()
        ctx = self._mock_response(
            {"status": "ok", "sidecar_id": "alpha-host", "entries_received": 0}
        )

        with (
            patch("scripts.sidecar_pkg.tls.build_context", return_value=None),
            patch("scripts.sidecar_pkg.transport.urlopen", return_value=ctx),
        ):
            sidecar._post_credential_manifest(
                api_url="https://api.example.com",
                api_key="test-key",
                sidecar_id="alpha-host",
                entries=[],
                on_resolved=callback,
            )

        callback.assert_not_called()

    def test_callback_exception_is_swallowed(self):
        """A buggy ``on_resolved`` callback must NOT take down the
        manifest POST path. Log and move on (defensive — the next
        ``/fleet/config`` will eventually heal any stale state)."""
        callback = MagicMock(side_effect=RuntimeError("callback boom"))
        ctx = self._mock_response(
            {
                "status": "ok",
                "sidecar_id": "alpha-host",
                "entries_received": 1,
                "resolved": {"anthropic": {"provider:anthropic": "a"}},
            }
        )

        with (
            patch("scripts.sidecar_pkg.tls.build_context", return_value=None),
            patch("scripts.sidecar_pkg.transport.urlopen", return_value=ctx),
        ):
            # Must not raise.
            sidecar._post_credential_manifest(
                api_url="https://api.example.com",
                api_key="test-key",
                sidecar_id="alpha-host",
                entries=[],
                on_resolved=callback,
            )

        callback.assert_called_once()


def test_run_collection_manifest_post_fires_even_when_some_provider_raises(monkeypatch):
    """PR #290 round-2 review (Hermes thread Vha0): the round-1
    ``if error_count > 0`` gate suppressed the WHOLE manifest, so a
    healthy provider's blocked origin was dropped while any single
    provider kept raising — the operator's Untagged surface went
    blind until that provider recovered. The fix scopes the prune
    per-provider via the existing ``try/except``: providers whose
    ``collect_provider`` raised contribute no entries to the
    manifest, so the server's ``delete_stale`` never sees their keys.
    The manifest POST must fire on a partial cycle so healthy
    providers' blocked origins still ship."""

    posted = {"called": False, "entries": [], "completed_providers": []}

    def _capture(*args, **kwargs):
        posted["called"] = True
        posted["entries"] = kwargs.get("entries", [])
        posted["completed_providers"] = kwargs.get("completed_providers", [])

    monkeypatch.setattr(sidecar, "_post_credential_manifest", _capture)

    # Empty the events providers list so the events loop is skipped.
    monkeypatch.setattr(sidecar, "_EVENT_PROVIDERS", frozenset())

    # Two providers in the registry, two different collect_provider
    # behaviors: one raises, one returns a blocked origin. The
    # manifest must still ship the healthy provider's blocked origin.
    def _by_provider(provider_id, *args, **kwargs):
        if provider_id == "anthropic":
            raise RuntimeError("anthropic exploded")
        return (
            [],
            [{"provider_id": provider_id, "credential_origin": f"provider:{provider_id}"}],
        )

    monkeypatch.setattr(sidecar.GenericCollector, "collect_provider", _by_provider)

    # Stub cache so the fetch path is a no-op.
    monkeypatch.setattr(sidecar, "_get_credential_cache", _StubCache)

    sidecar.run_collection(
        config={"api_url": "http://x", "api_key": "k"},
        providers=["anthropic", "chatgpt"],
    )

    assert posted["called"], (
        "manifest POST must fire on partial cycles so healthy providers' "
        "blocked origins still ship (PR #290 round-2 Hermes thread Vha0)"
    )
    # The healthy provider's blocked origin made it through; the
    # raised provider's did not.
    assert posted["entries"] == [
        {"provider_id": "chatgpt", "credential_origin": "provider:chatgpt"}
    ]
    assert posted["completed_providers"] == ["chatgpt"]


def test_run_collection_manifest_post_consumes_resolved_into_cache(monkeypatch, tmp_path):
    """End-to-end: the manifest POST's ``resolved`` payload is merged
    into the persistent cache. The next ``run_collection`` cycle
    consults that cache to unblock the now-tagged card (PR #290
    round-2 review, Hermes suggestion #9 — same-cycle loop closure)."""
    from scripts.sidecar_pkg.credentials import CredentialCache

    cache = CredentialCache()
    cache.replace(
        accounts={"anthropic": ["alice@example.com"]},
        tokens={},
        tag_hints={},  # No hints yet — first time the operator tags.
    )
    monkeypatch.setattr(sidecar, "_CREDENTIAL_CACHE", cache)
    monkeypatch.setattr(sidecar, "_get_credential_cache", lambda: cache)

    # Empty the events providers list — the events loop isn't the
    # subject of this test and would otherwise raise on missing
    # ``_LEGACY_EVENT_ACCOUNT_DISCOVERY[provider_id]``.
    monkeypatch.setattr(sidecar, "_EVENT_PROVIDERS", frozenset())

    posted: dict[str, Any] = {}

    def _capture_manifest(*, api_url, api_key, sidecar_id, entries, on_resolved, **kw):
        posted["entries"] = entries
        # Simulate the server returning the operator's just-set tag.
        on_resolved({"anthropic": {"provider:anthropic": "alice@example.com"}})

    monkeypatch.setattr(sidecar, "_post_credential_manifest", _capture_manifest)

    # collect_provider returns a blocked origin (no local discovery).
    monkeypatch.setattr(
        sidecar.GenericCollector,
        "collect_provider",
        lambda *a, **kw: (
            [],
            [{"provider_id": "anthropic", "credential_origin": "provider:anthropic"}],
        ),
    )

    sidecar.run_collection(
        config={"api_url": "http://x", "api_key": "k"},
        providers=["anthropic"],
    )

    # The manifest fired with the blocked origin.
    assert posted["entries"] == [
        {"provider_id": "anthropic", "credential_origin": "provider:anthropic"}
    ]
    # And the cache now carries the operator's tag — the next cycle's
    # block-guard will see this and unblock the card without waiting
    # on the next /fleet/config round-trip.
    assert cache.provider_tag_hints() == {
        "anthropic": {"provider:anthropic": "alice@example.com"},
    }


# ---------------------------------------------------------------------------
# Events-path tag-hint application + untagged reporting (PR for MiniMax
# card-split). Mirrors TestRunCollectionManifestPostConsumesResolvedIntoCache
# above for the events branch: server tag-hints retarget events onto the
# operator's chosen account_id, and no-resolvable-identity events surface
# in the same Untagged Credentials dialog.
# ---------------------------------------------------------------------------


class TestResolvedHintsReplace:
    """The server's answer for a provider is complete, so the sidecar replaces
    what it holds for that provider instead of merging into it (#874).

    A merge never deletes a key, so a hint the sidecar cached before the
    account was archived survived until the next /fleet/config replaced the
    whole map — "not shipped" was not "not applied".
    """

    def test_a_retracted_hint_is_dropped(self):
        cached = {"kimi_coding": {"path:/home/u/.kimi/credentials.json": "old@example.com"}}
        resolved = {"kimi_coding": {}}

        merged = sidecar._apply_resolved_hints(cached, resolved)

        # The provider is gone, not left behind with an empty bucket: the
        # cache equals what the sidecar holds, so the provider is no longer
        # named in `hint_providers` and not asked about again.
        assert "kimi_coding" not in merged

    def test_a_retracted_provider_is_not_asked_about_again(self):
        """An emptied provider leaves `hint_providers`, so the server is not
        asked for a hint the sidecar no longer holds."""
        cached = {
            "kimi_coding": {"path:/home/u/.kimi/credentials.json": "old@example.com"},
            "anthropic": {"provider:anthropic": "alice@example.com"},
        }
        resolved = {"kimi_coding": {}}

        merged = sidecar._apply_resolved_hints(cached, resolved)

        assert sorted(merged) == ["anthropic"]

    def test_a_new_hint_lands_on_the_same_cycle(self):
        cached = {"anthropic": {"provider:anthropic": "alice@example.com"}}
        resolved = {
            "anthropic": {"provider:anthropic": "alice@example.com"},
            "chatgpt": {"provider:chatgpt": "alice@example.com"},
        }

        merged = sidecar._apply_resolved_hints(cached, resolved)

        assert merged["anthropic"] == {"provider:anthropic": "alice@example.com"}
        assert merged["chatgpt"] == {"provider:chatgpt": "alice@example.com"}

    def test_a_provider_the_server_did_not_answer_for_keeps_its_hints(self):
        cached = {"kimi_coding": {"path:/home/u/.kimi/credentials.json": "old@example.com"}}
        resolved = {"anthropic": {"provider:anthropic": "alice@example.com"}}

        merged = sidecar._apply_resolved_hints(cached, resolved)

        assert merged["kimi_coding"] == {"path:/home/u/.kimi/credentials.json": "old@example.com"}

    def test_the_cached_map_is_not_mutated(self):
        cached = {"kimi_coding": {"path:/home/u/.kimi/credentials.json": "old@example.com"}}

        merged = sidecar._apply_resolved_hints(cached, {"kimi_coding": {}})

        assert cached["kimi_coding"] == {"path:/home/u/.kimi/credentials.json": "old@example.com"}
        assert merged is not cached

    def test_the_manifest_names_the_providers_whose_hints_it_holds(self):
        """The server answers for the providers the sidecar names, so a
        provider whose tag was deleted (no tag row left to make it a
        candidate) still gets an answer."""
        captured: dict = {}

        def _capture(body: bytes) -> None:
            captured.update(json.loads(body.decode("utf-8")))

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(
            return_value=MagicMock(
                getcode=MagicMock(return_value=200), read=MagicMock(return_value=b"{}")
            )
        )
        ctx.__exit__ = MagicMock(return_value=False)

        with (
            patch("scripts.sidecar_pkg.tls.build_context", return_value=None),
            patch(
                "scripts.sidecar_pkg.transport.urlopen",
                side_effect=lambda *a, **kw: _capture(a[0].data) or ctx,
            ),
        ):
            sidecar._post_credential_manifest(
                api_url="https://api.example.com",
                api_key="test-key",
                sidecar_id="alpha-host",
                entries=[],
                hint_providers=["kimi_coding", "anthropic"],
            )

        assert captured["hint_providers"] == ["anthropic", "kimi_coding"]

    def test_the_named_providers_are_truncated_to_the_servers_cap(self):
        """An oversized list must not fail the whole manifest.

        The server rejects a manifest naming more providers than its cap
        allows, and a rejected manifest takes this cycle's
        `completed_providers` with it — the server would then stop pruning
        this host's stale pending rows. Truncating degrades to fewer answers.
        """
        captured: dict = {}

        def _capture(body: bytes) -> None:
            captured.update(json.loads(body.decode("utf-8")))

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(
            return_value=MagicMock(
                getcode=MagicMock(return_value=200), read=MagicMock(return_value=b"{}")
            )
        )
        ctx.__exit__ = MagicMock(return_value=False)

        named = [f"provider_{i}" for i in range(sidecar._MAX_HINT_PROVIDERS + 5)]

        with (
            patch("scripts.sidecar_pkg.tls.build_context", return_value=None),
            patch(
                "scripts.sidecar_pkg.transport.urlopen",
                side_effect=lambda *a, **kw: _capture(a[0].data) or ctx,
            ),
        ):
            sidecar._post_credential_manifest(
                api_url="https://api.example.com",
                api_key="test-key",
                sidecar_id="alpha-host",
                entries=[],
                hint_providers=named,
            )

        # The report still goes out, with fewer names than asked for.
        assert captured["sidecar_id"] == "alpha-host"
        assert captured["hint_providers"] == sorted(named)[: sidecar._MAX_HINT_PROVIDERS]
