"""Key-scoped credential origins for OpenCode's sibling providers (#349).

OpenCode's ``auth.json`` holds one bare key per backend and nothing that
says whose key it is (see ``tests/unit/test_opencode_credential_identity.py``
for the #347 problem statement). Five siblings read the same file —
``openrouter``, ``minimax``, ``kimi_coding``, ``ollama`` and ``xai`` — so
they inherit it verbatim: ``path:/home/u/.local/share/opencode/auth.json``
is the same string on every host with the same username, and the same
string before and after a rotation.

These tests pin the extension:

- one shared provider set, mirrored on both sides of the fleet boundary;
- every *key* candidate a sibling produces is fingerprinted at its origin —
  env rule, file rule, and xai's non-``api_key`` fields (its rules map the
  bearer to ``xai_access``, and the *refresh* token wins whenever the
  candidate carries one, because the access JWT rotates weekly);
- non-key candidates (cookies, CLI-OAuth tokens, cosmetic env vars) keep
  their plain origins, so nothing an operator already tagged moves;
- the cascade resolves a sibling's keyed origin the way opencode's
  resolves, including the server's path-independent
  ``provider:<pid>#<fp>`` hint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.sidecar as sc
from app.services.account_identity import FINGERPRINTED_ORIGIN_PROVIDERS as SERVER_SET
from scripts.sidecar_pkg.identity import (
    FINGERPRINTED_ORIGIN_PROVIDERS as SIDECAR_SET,
)
from scripts.sidecar_pkg.identity import (
    credential_fingerprint,
)

SIBLINGS = ("openrouter", "minimax", "kimi_coding", "ollama", "xai")

# (provider, env variable, credential value) — the env half of each sibling's
# key discovery. xai's bearer arrives under ``xai_access``; the rest under
# ``api_key``.
ENV_CASES = [
    ("openrouter", "OPENROUTER_API_KEY", "sk-or-sibling-key"),  # pragma: allowlist secret
    ("minimax", "MINIMAX_API_KEY", "mm-sibling-key"),  # pragma: allowlist secret
    ("kimi_coding", "KIMI_CODE_API_KEY", "kc-sibling-key"),  # pragma: allowlist secret
    ("ollama", "OLLAMA_API_KEY", "ol-sibling-key"),  # pragma: allowlist secret
    ("xai", "GROK_OAUTH_TOKEN", "xai-sibling-bearer"),  # pragma: allowlist secret
]

# (provider, JSON payload, credential value) — what each sibling's OpenCode
# ``auth.json`` file rule reads out of the same shared file, and the field
# whose value must end up in the fingerprint. xai gets two entries because
# the production payload carries *both* tokens: the refresh one wins, and
# the access-only shape is the fallback for a file that has no refresh.
FILE_CASES = [
    (
        "openrouter",
        {"openrouter": {"key": "sk-or-file-key"}},
        "sk-or-file-key",
    ),  # pragma: allowlist secret
    (
        "minimax",
        {"minimax-coding-plan": {"key": "mm-file-key"}},
        "mm-file-key",
    ),  # pragma: allowlist secret
    (
        "kimi_coding",
        {"kimi-code-plan-global": {"key": "kc-file-key"}},
        "kc-file-key",
    ),  # pragma: allowlist secret
    ("ollama", {"ollama-cloud": {"key": "ol-file-key"}}, "ol-file-key"),  # pragma: allowlist secret
    (
        "xai",
        {"xai": {"access": "xai-file-bearer"}},
        "xai-file-bearer",
    ),  # pragma: allowlist secret
    (
        "xai",
        {"xai": {"access": "xai-file-bearer", "refresh": "xai-file-refresh"}},
        "xai-file-refresh",
    ),  # pragma: allowlist secret
]
FILE_CASE_IDS = [
    "openrouter",
    "minimax",
    "kimi_coding",
    "ollama",
    "xai-access-only",
    "xai-with-refresh",
]


def _rule(pid: str, rule_type: str, **match: Any) -> dict[str, Any]:
    """One rule from the sidecar's baked registry, filtered by extra fields.

    Reading the rules from the registry (rather than hand-writing a config)
    means these tests break if a sibling's mapping target drifts — which is
    exactly what the ``xai_access`` case exists to catch.
    """
    for rule in sc.__REGISTRY__["providers"][pid]["rules"]:
        if rule.get("type") != rule_type:
            continue
        if all(rule.get(key) == value for key, value in match.items()):
            return rule
    raise AssertionError(f"no {rule_type} rule {match} for {pid}")


def _opencode_auth_rule(pid: str) -> dict[str, Any]:
    """The sibling's file rule that reads OpenCode's ``auth.json``."""
    for rule in sc.__REGISTRY__["providers"][pid]["rules"]:
        if rule.get("type") == "file" and any(
            "opencode/auth.json" in path for path in rule.get("paths", [])
        ):
            return rule
    raise AssertionError(f"no OpenCode auth.json file rule for {pid}")


def _collect(pid: str, rules: list[dict[str, Any]], **kwargs: Any):
    config = {"name": pid, "icon": "🔑", "rules": rules}
    return sc.GenericCollector.collect_provider(pid, config, **kwargs)


class TestSharedProviderSet:
    def test_set_is_mirrored_on_both_sides_of_the_fleet_boundary(self) -> None:
        """The sidecar needs the set to suffix origins, the server needs it
        to answer ``provider:<pid>#<fp>`` hints. Two copies that disagree
        would silently drop tier-1b hints for half the fleet."""
        expected = frozenset({"opencode", "openrouter", "minimax", "kimi_coding", "ollama", "xai"})
        assert SERVER_SET == expected
        assert SIDECAR_SET == expected

    def test_scope_excludes_kimi_and_identity_bearing_providers(self) -> None:
        """#349's explicit non-goals, pinned so a later PR has to change
        this test on purpose: ``kimi`` is out of scope, and providers whose
        credential carries a real identity were never in it."""
        assert SERVER_SET - {"opencode"} == frozenset(SIBLINGS)
        for pid in ("kimi", "anthropic", "chatgpt", "gemini"):
            assert pid not in SERVER_SET
            assert pid not in SIDECAR_SET

    def test_every_keyed_provider_is_registered(self) -> None:
        """Registration only: a provider in the set that neither registry
        knows would never produce a keyed origin. What each registry's *rules*
        look like is #351's problem, deliberately not asserted here."""
        registry = json.loads(
            (Path(__file__).resolve().parents[2] / "app" / "core" / "registry.json").read_text()
        )
        for pid in SERVER_SET:
            assert pid in sc.__REGISTRY__["providers"], pid
            assert pid in registry["providers"], pid

    def test_registry_maps_each_sibling_key_to_the_field_we_fingerprint(self) -> None:
        """The gotcha behind ``_FINGERPRINT_KEY_FIELDS``: xai's rules write
        the bearer to ``xai_access`` — never ``api_key``, which would
        fingerprint nothing — and additionally map the refresh token the
        origin prefers over that rotating bearer."""
        for pid in ("openrouter", "minimax", "kimi_coding", "ollama"):
            assert set(_opencode_auth_rule(pid)["mapping"].values()) == {"api_key"}
        assert _opencode_auth_rule("xai")["mapping"] == {
            "xai.access": "xai_access",
            "xai.refresh": "xai_refresh",
        }
        assert _rule("xai", "env", variable="GROK_OAUTH_TOKEN")["mapping"] == {
            "value": "xai_access"
        }


class TestKeyedOrigins:
    @pytest.mark.parametrize(("pid", "variable", "value"), ENV_CASES)
    def test_env_origin_carries_the_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch, pid: str, variable: str, value: str
    ) -> None:
        monkeypatch.setenv(variable, value)

        _, blocked = _collect(pid, [_rule(pid, "env", variable=variable)])

        assert blocked == [
            {
                "provider_id": pid,
                "credential_origin": f"env:{variable}#{credential_fingerprint(value)}",
            }
        ]

    @pytest.mark.parametrize(("pid", "payload", "value"), FILE_CASES, ids=FILE_CASE_IDS)
    def test_file_origin_carries_the_fingerprint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pid: str,
        payload: dict[str, Any],
        value: str,
    ) -> None:
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(sc, "expand_file_rule_paths", lambda _paths: [auth])

        _, blocked = _collect(pid, [_opencode_auth_rule(pid)])

        assert blocked == [
            {
                "provider_id": pid,
                "credential_origin": f"path:{auth.resolve()}#{credential_fingerprint(value)}",
            }
        ]

    def test_rotation_produces_a_new_origin_at_the_same_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the bug: the *same* descriptor before and after
        a key rotation. The path is unchanged, so only the fingerprint can
        tell the two credentials apart — and it does."""
        auth = tmp_path / "auth.json"
        rule = _opencode_auth_rule("openrouter")
        monkeypatch.setattr(sc, "expand_file_rule_paths", lambda _paths: [auth])

        auth.write_text(
            json.dumps({"openrouter": {"key": "sk-or-key-old"}}), encoding="utf-8"
        )  # pragma: allowlist secret
        _, blocked_old = _collect("openrouter", [rule])
        auth.write_text(
            json.dumps({"openrouter": {"key": "sk-or-key-new"}}), encoding="utf-8"
        )  # pragma: allowlist secret
        _, blocked_new = _collect("openrouter", [rule])

        base = f"path:{auth.resolve()}"
        assert blocked_old[0]["credential_origin"] == (
            f"{base}#{credential_fingerprint('sk-or-key-old')}"
        )
        assert blocked_new[0]["credential_origin"] == (
            f"{base}#{credential_fingerprint('sk-or-key-new')}"
        )
        assert blocked_old[0]["credential_origin"] != blocked_new[0]["credential_origin"]


class TestXaiRefreshPreference:
    """xai is the sibling whose key rotates under Runway's feet.

    Its access JWT expires in about seven days and the Grok / OpenCode CLI
    refreshes it — Runway never does — so fingerprinting that field would
    mint a new origin every week and strand whatever tag the operator
    wrote. The refresh token wins whenever the candidate carries one, and
    the access-only ``GROK_OAUTH_TOKEN`` candidate falls back to its bearer
    because it has nothing else to identify it by.
    """

    ACCESS_OLD = "xai-access-jwt-old"  # pragma: allowlist secret
    ACCESS_NEW = "xai-access-jwt-new"  # pragma: allowlist secret
    REFRESH = "xai-refresh-token"  # pragma: allowlist secret

    def _xai_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
    ) -> tuple[Path, str]:
        """Write ``payload`` as the OpenCode ``auth.json`` and return the
        path plus the one xai origin the file rule derives from it."""
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(sc, "expand_file_rule_paths", lambda _paths: [auth])
        _, blocked = _collect("xai", [_opencode_auth_rule("xai")])
        assert len(blocked) == 1, blocked
        return auth, blocked[0]["credential_origin"]

    def test_refresh_token_wins_over_the_access_bearer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, origin = self._xai_file(
            tmp_path, monkeypatch, {"xai": {"access": self.ACCESS_OLD, "refresh": self.REFRESH}}
        )

        assert origin.endswith(f"#{credential_fingerprint(self.REFRESH)}")
        assert not origin.endswith(f"#{credential_fingerprint(self.ACCESS_OLD)}")

    def test_origin_survives_an_access_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure this preference exists to prevent: the file, the path
        and the refresh token are unchanged, the CLI rotates the access
        JWT — and the origin, and therefore the tag on it, must not move."""
        _, origin_before = self._xai_file(
            tmp_path, monkeypatch, {"xai": {"access": self.ACCESS_OLD, "refresh": self.REFRESH}}
        )
        _, origin_after = self._xai_file(
            tmp_path, monkeypatch, {"xai": {"access": self.ACCESS_NEW, "refresh": self.REFRESH}}
        )

        assert origin_after == origin_before

    def test_access_only_candidate_falls_back_to_the_bearer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GROK_OAUTH_TOKEN`` maps to ``xai_access`` alone — with no
        refresh token in the dict there is still a credential to scope, so
        the origin stays keyed rather than silently going plain."""
        _, origin = self._xai_file(tmp_path, monkeypatch, {"xai": {"access": self.ACCESS_OLD}})

        assert origin.endswith(f"#{credential_fingerprint(self.ACCESS_OLD)}")

    def test_cascade_hint_is_built_from_the_refresh_fingerprint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 1b for xai: the server hashes whatever it was given, so the
        hint the cascade can consume is the one keyed to the same field the
        origin is — a hint keyed to the rotating access bearer is for a
        different credential and must not resolve."""
        _, origin = self._xai_file(
            tmp_path, monkeypatch, {"xai": {"access": self.ACCESS_OLD, "refresh": self.REFRESH}}
        )
        refresh_hint = {f"provider:xai#{credential_fingerprint(self.REFRESH)}": "grok@example.com"}

        cards, blocked = _collect(
            "xai",
            [_opencode_auth_rule("xai")],
            account_label_hints={"xai": refresh_hint},
        )
        assert blocked == []
        assert [card["account_id"] for card in cards] == ["grok@example.com"]

        cards, blocked = _collect(
            "xai",
            [_opencode_auth_rule("xai")],
            account_label_hints={
                "xai": {f"provider:xai#{credential_fingerprint(self.ACCESS_OLD)}": "wrong@x.com"}
            },
        )
        assert cards == []
        assert blocked[0]["credential_origin"] == origin


class TestSiblingCascade:
    """The #347 cascade is generic: it splits the fingerprint back off the
    origin and rebuilds the server's path-independent key from it, so a
    sibling resolves exactly like opencode does."""

    KEY = "sk-or-cascade-key"  # pragma: allowlist secret
    OTHER_KEY = "sk-or-other-cascade-key"  # pragma: allowlist secret

    def _seed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"openrouter": {"key": self.KEY}}), encoding="utf-8")
        monkeypatch.setattr(sc, "expand_file_rule_paths", lambda _paths: [auth])
        _, blocked = _collect("openrouter", [_opencode_auth_rule("openrouter")])
        assert blocked[0]["credential_origin"] == (
            f"path:{auth.resolve()}#{credential_fingerprint(self.KEY)}"
        )
        return auth

    def _hints(self, pid: str, hints: dict[str, str]) -> dict[str, dict[str, str]]:
        return {pid: hints}

    def test_fingerprint_hint_unblocks_via_provider_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 1b: the server cannot know this host's path, so it ships the
        hint under ``provider:<pid>#<fp>`` — the cascade rebuilds the same
        key from the fingerprint it just split off."""
        self._seed(tmp_path, monkeypatch)
        fp = credential_fingerprint(self.KEY)

        cards, blocked = _collect(
            "openrouter",
            [_opencode_auth_rule("openrouter")],
            account_label_hints=self._hints(
                "openrouter", {f"provider:openrouter#{fp}": "alice@example.com"}
            ),
        )

        assert blocked == []
        assert [card["account_id"] for card in cards] == ["alice@example.com"]

    def test_fingerprint_hint_for_another_key_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Isolation: a hint keyed to somebody else's credential must not
        resolve this one, even behind the same provider descriptor."""
        auth = self._seed(tmp_path, monkeypatch)

        cards, blocked = _collect(
            "openrouter",
            [_opencode_auth_rule("openrouter")],
            account_label_hints=self._hints(
                "openrouter",
                {
                    f"provider:openrouter#{credential_fingerprint(self.OTHER_KEY)}": "bob@example.com"
                },
            ),
        )

        assert cards == []
        assert blocked[0]["credential_origin"] == (
            f"path:{auth.resolve()}#{credential_fingerprint(self.KEY)}"
        )

    def test_legacy_plain_origin_tag_still_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 2/3: tags written before a sibling's origins were keyed keep
        working — the cascade falls back to the un-keyed descriptor."""
        auth = self._seed(tmp_path, monkeypatch)

        cards, blocked = _collect(
            "openrouter",
            [_opencode_auth_rule("openrouter")],
            account_label_hints=self._hints(
                "openrouter", {f"path:{auth.resolve()}": "legacy@x.com"}
            ),
        )

        assert blocked == []
        assert [card["account_id"] for card in cards] == ["legacy@x.com"]


class TestNonKeyCandidatesStayPlain:
    def test_cosmetic_env_vars_keep_their_plain_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``openrouter`` ships two cosmetic env rules alongside its key.
        Neither is a credential, and neither carries ``api_key`` — they keep
        the plain descriptor."""
        monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://example.com")
        monkeypatch.setenv("OPENROUTER_X_TITLE", "runway")

        _, blocked = _collect(
            "openrouter",
            [
                _rule("openrouter", "env", variable="OPENROUTER_HTTP_REFERER"),
                _rule("openrouter", "env", variable="OPENROUTER_X_TITLE"),
            ],
        )

        origins = {entry["credential_origin"] for entry in blocked}
        assert origins == {"env:OPENROUTER_HTTP_REFERER", "env:OPENROUTER_X_TITLE"}

    def test_cookie_candidates_keep_their_plain_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cookie discovery is out of #349's scope: a browser session has an
        identity upstream, and it carries no key field to fingerprint."""
        monkeypatch.setattr(
            sc.BrowserCookieExtractor, "get_cookie", staticmethod(lambda *_args, **_kwargs: "sess")
        )

        _, blocked = _collect("ollama", [_rule("ollama", "cookie")])

        assert blocked == [{"provider_id": "ollama", "credential_origin": "cookie:ollama/session"}]

    def test_cli_oauth_file_keeps_its_plain_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``kimi_coding``'s own credentials file is keyed to a *user*, not
        to a bare API key — it stays plain, as does its plain-env twin."""
        cli_file = tmp_path / "kimi-code-abc.json"
        cli_file.write_text(json.dumps({"access_token": "cli-token"}), encoding="utf-8")
        monkeypatch.setattr(sc, "expand_file_rule_paths", lambda _paths: [cli_file])
        monkeypatch.delenv("KIMI_CODE_API_KEY", raising=False)

        _, blocked = _collect(
            "kimi_coding",
            [
                rule
                for rule in sc.__REGISTRY__["providers"]["kimi_coding"]["rules"]
                if rule.get("type") == "file" and "kimi-code" in str(rule.get("paths"))
            ],
        )

        assert blocked == [
            {"provider_id": "kimi_coding", "credential_origin": f"path:{cli_file.resolve()}"}
        ]
