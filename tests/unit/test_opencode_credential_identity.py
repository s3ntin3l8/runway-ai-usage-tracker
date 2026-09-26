"""OpenCode CLI credential identity — key-scoped origins + the #347 cascade.

The OpenCode CLI stores a bare API key with no per-account identity of its
own: no email, no user id, and no online endpoint that answers "whose key is
this?" (the source census in ``docs/collectors/opencode.md`` documents every
path checked and coming back empty). The sidecar therefore cannot *derive* an
account for it — but it must never *inherit* one either.

``path:/home/u/.local/share/opencode/auth.json`` is the same string on every
host with the same username, and the same string before and after a key
rotation. Were that descriptor the origin, two different keys would share one
operator tag and silently land on each other's account.

These tests pin the fix in two halves:

- the origin itself is suffixed with a fingerprint of the discovered value,
  so it identifies the *credential* rather than the file it was found in;
- the token-card cascade resolves that origin most-specific-first, and the
  provider-wide auto-hint (the last tier) is gated on local ``account.json``
  state that is allowed to contradict it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import scripts.sidecar as sc
from scripts.sidecar_pkg.identity import credential_fingerprint, split_keyed_origin

KEY_A = "oc_sk_test_key"  # pragma: allowlist secret — fingerprint pinned in FP_A
KEY_B = "oc_sk_other_key"  # pragma: allowlist secret
KEY_ACTIVE = "oc_sk_rotated_active"  # pragma: allowlist secret
FP_A = credential_fingerprint(KEY_A)
FP_B = credential_fingerprint(KEY_B)


@pytest.fixture(autouse=True)
def _isolate_opencode_cli_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the ``account.json`` reader at an empty temp dir.

    Autouse because the reader's default scans ``~/.local/share/opencode``
    and ``~/.opencode`` — a test that read the machine's real CLI state would
    be non-deterministic, and a test that *leaked* it would put a real
    operator's key descriptor into an assertion message. Individual tests
    override this to install the state they care about.
    """
    monkeypatch.setattr(sc, "_opencode_account_json_paths", lambda: [])
    monkeypatch.delenv("OPENCODE_ACCOUNT_LABEL", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    return tmp_path


def _write_auth(directory: Path, key: str) -> Path:
    """Write an OpenCode CLI ``auth.json`` holding one ``opencode-go`` key."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "auth.json"
    path.write_text(
        json.dumps({"opencode-go": {"type": "api", "key": key}}),
        encoding="utf-8",
    )
    return path


def _write_account_json(directory: Path, active_key: str) -> Path:
    """Write an ``account.json`` whose active ``opencode-go`` record carries
    ``active_key`` — the only local evidence that can contradict the key in
    ``auth.json``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "account.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "accounts": {
                    "acct-live": {
                        "id": "acct-live",
                        "serviceID": "opencode-go",
                        "description": "default",
                        "credential": {"type": "api", "key": active_key},
                    }
                },
                "active": {"opencode-go": "acct-live"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _file_config(auth_path: Path) -> dict[str, Any]:
    """An opencode provider config carrying only the registry's file rule.

    The registry's sqlite rule is deliberately left out: it needs a real
    ``opencode.db`` and would emit quota cards that drown out the single
    token card these tests assert on.
    """
    return {
        "name": "OpenCode",
        "icon": "⚡",
        "rules": [
            {
                "type": "file",
                "paths": [str(auth_path)],
                "format": "json",
                "mapping": {"opencode-go.key": "api_key"},
            }
        ],
    }


def _env_config() -> dict[str, Any]:
    return {
        "name": "OpenCode",
        "icon": "⚡",
        "rules": [
            {
                "type": "env",
                "variable": "OPENCODE_API_KEY",
                "mapping": {"value": "api_key"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Key-scoped origins
# ---------------------------------------------------------------------------


class TestKeyedCredentialOrigins:
    def test_file_rule_origin_carries_the_key_fingerprint(self, tmp_path: Path) -> None:
        """An untagged key blocks, and the origin it blocks under identifies
        the credential — not the file it was read from."""
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider("opencode", _file_config(auth))

        assert cards == []
        assert blocked == [
            {
                "provider_id": "opencode",
                "credential_origin": f"path:{auth.resolve()}#{FP_A}",
            }
        ]

    def test_env_rule_origin_carries_the_key_fingerprint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCODE_API_KEY", KEY_A)

        _, blocked = sc.GenericCollector.collect_provider("opencode", _env_config())

        assert blocked == [
            {"provider_id": "opencode", "credential_origin": f"env:OPENCODE_API_KEY#{FP_A}"}
        ]

    def test_two_keys_produce_two_origins(self, tmp_path: Path) -> None:
        """Isolation: same descriptor shape, different credentials, different
        origins — so one's operator tag can never be inherited by the other."""
        auth_a = _write_auth(tmp_path / "host-a", KEY_A)
        auth_b = _write_auth(tmp_path / "host-b", KEY_B)

        _, blocked_a = sc.GenericCollector.collect_provider("opencode", _file_config(auth_a))
        _, blocked_b = sc.GenericCollector.collect_provider("opencode", _file_config(auth_b))

        origin_a = blocked_a[0]["credential_origin"]
        origin_b = blocked_b[0]["credential_origin"]
        assert origin_a == f"path:{auth_a.resolve()}#{FP_A}"
        assert origin_b == f"path:{auth_b.resolve()}#{FP_B}"
        assert origin_a != origin_b

    def test_split_round_trips_the_keyed_origin(self, tmp_path: Path) -> None:
        """The cascade's first step is splitting the fingerprint back off so
        it can rebuild the server's ``provider:`` key."""
        auth = _write_auth(tmp_path, KEY_A)
        _, blocked = sc.GenericCollector.collect_provider("opencode", _file_config(auth))

        base, fingerprint = split_keyed_origin(blocked[0]["credential_origin"])
        assert base == f"path:{auth.resolve()}"
        assert fingerprint == FP_A

    def test_fingerprint_is_kept_for_non_keyed_providers(self) -> None:
        """The helper is generic but the *use* is gated on
        ``FINGERPRINTED_ORIGIN_PROVIDERS`` (#349): a provider outside that
        set keeps its plain descriptor so no existing operator tag moves,
        and so does a keyed provider whose candidate simply carries no key
        (a cookie, ``openrouter``'s cosmetic env vars, …) — a candidate
        with nothing to fingerprint stays plain by construction."""
        assert (
            sc.fingerprinted_credential_origin(
                "provider:anthropic", "anthropic", {"api_key": KEY_A}
            )
            == "provider:anthropic"
        )
        assert (
            sc.fingerprinted_credential_origin("provider:opencode", "opencode", {})
            == "provider:opencode"
        )
        assert (
            sc.fingerprinted_credential_origin(
                "env:OPENROUTER_HTTP_REFERER", "openrouter", {"http_referer": "https://x"}
            )
            == "env:OPENROUTER_HTTP_REFERER"
        )


# ---------------------------------------------------------------------------
# The token-card cascade
# ---------------------------------------------------------------------------


class TestTokenCardCascade:
    def test_keyed_operator_tag_unblocks(self, tmp_path: Path) -> None:
        """Tier 1: a tag written against this exact credential wins."""
        auth = _write_auth(tmp_path, KEY_A)
        origin = f"path:{auth.resolve()}#{FP_A}"

        cards, blocked = sc.GenericCollector.collect_provider(
            "opencode",
            _file_config(auth),
            account_label_hints={"opencode": {origin: "alice@example.com"}},
        )

        assert blocked == []
        assert [c["account_id"] for c in cards] == ["alice@example.com"]

    def test_fingerprint_hint_unblocks_via_provider_descriptor(self, tmp_path: Path) -> None:
        """Tier 1b: the server cannot know our filesystem layout, so it ships
        its hint under ``provider:opencode#<fp>``. The cascade derives the
        same key from the fingerprint it just split off."""
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider(
            "opencode",
            _file_config(auth),
            account_label_hints={"opencode": {f"provider:opencode#{FP_A}": "alice@example.com"}},
        )

        assert blocked == []
        assert [c["account_id"] for c in cards] == ["alice@example.com"]

    def test_fingerprint_hint_for_a_different_key_is_ignored(self, tmp_path: Path) -> None:
        """Isolation: a hint keyed to another credential must not resolve this
        one, even though both sit behind the same provider descriptor."""
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider(
            "opencode",
            _file_config(auth),
            account_label_hints={"opencode": {f"provider:opencode#{FP_B}": "bob@example.com"}},
        )

        assert cards == []
        assert blocked[0]["credential_origin"] == f"path:{auth.resolve()}#{FP_A}"

    def test_legacy_plain_origin_tag_still_resolves(self, tmp_path: Path) -> None:
        """Tier 2: tags written before origins were fingerprinted keep
        working — the cascade falls back to the un-keyed descriptor."""
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider(
            "opencode",
            _file_config(auth),
            account_label_hints={"opencode": {f"path:{auth.resolve()}": "alice@example.com"}},
        )

        assert blocked == []
        assert [c["account_id"] for c in cards] == ["alice@example.com"]

    def test_fingerprint_hint_beats_a_legacy_plain_tag(self, tmp_path: Path) -> None:
        """Specificity decides the order, not chronology: a key-exact hint
        outranks a path-only tag, because the path tag is the origin that
        still inherits across hosts and rotations (the debt to clear)."""
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider(
            "opencode",
            _file_config(auth),
            account_label_hints={
                "opencode": {
                    f"path:{auth.resolve()}": "legacy@example.com",
                    f"provider:opencode#{FP_A}": "alice@example.com",
                }
            },
        )

        assert blocked == []
        assert [c["account_id"] for c in cards] == ["alice@example.com"]

    def test_env_label_stamps_locally_and_beats_hints(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Tier 0 (T2): an explicit host-level label is local evidence and
        outranks every server-side hint — the same precedence
        ``_opencode_account_email`` already applies to events."""
        monkeypatch.setenv("OPENCODE_ACCOUNT_LABEL", "Work@Example.com")
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider(
            "opencode",
            _file_config(auth),
            account_label_hints={"opencode": {f"provider:opencode#{FP_A}": "alice@example.com"}},
        )

        assert blocked == []
        assert [c["account_id"] for c in cards] == ["work@example.com"]

    def test_unhinted_key_blocks_under_a_key_scoped_origin(self, tmp_path: Path) -> None:
        """The default for an unknown credential: nothing ships, and what the
        manifest reports still names the credential."""
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider("opencode", _file_config(auth))

        assert cards == []
        assert blocked == [
            {"provider_id": "opencode", "credential_origin": f"path:{auth.resolve()}#{FP_A}"}
        ]


# ---------------------------------------------------------------------------
# Tier 3 — the gated provider-wide auto-hint
# ---------------------------------------------------------------------------


class TestProviderWideHintGate:
    """``provider:<pid>`` is the last resort, and for opencode only when
    local ``account.json`` state does not contradict it."""

    @staticmethod
    def _auto_hint() -> dict[str, dict[str, str]]:
        return {"opencode": {"provider:opencode": "alice@example.com"}}

    @pytest.mark.parametrize(
        ("active_key", "expected_cards"),
        [
            (None, ["alice@example.com"]),  # no account.json at all → unknown
            (KEY_A, ["alice@example.com"]),  # active record agrees → single
        ],
        ids=["no-local-state", "local-state-agrees"],
    )
    def test_auto_hint_applies(
        self, tmp_path: Path, active_key: str | None, expected_cards: list[str]
    ) -> None:
        if active_key is not None:
            _write_account_json(tmp_path, active_key)
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider(
            "opencode",
            _file_config(auth),
            account_label_hints=self._auto_hint(),
        )

        assert blocked == []
        assert [c["account_id"] for c in cards] == expected_cards

    def test_auto_hint_withheld_when_local_state_disagrees(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The credential on disk is not the one the CLI says is active.
        Associating it with whatever single account the server has configured
        is exactly the guess #347 exists to prevent — the card stays blocked
        and surfaces as Untagged."""
        account_path = _write_account_json(tmp_path, KEY_ACTIVE)
        monkeypatch.setattr(sc, "_opencode_account_json_paths", lambda: [account_path])
        auth = _write_auth(tmp_path, KEY_A)

        with caplog.at_level(logging.WARNING):
            cards, blocked = sc.GenericCollector.collect_provider(
                "opencode",
                _file_config(auth),
                account_label_hints=self._auto_hint(),
            )

        assert cards == []
        assert blocked[0]["credential_origin"] == f"path:{auth.resolve()}#{FP_A}"
        assert "provider-wide account hint withheld" in caplog.text

    def test_key_scoped_tag_still_resolves_when_the_gate_withholds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Withholding the guess must not dead-end the credential: tagging
        the key-scoped origin still unblocks it."""
        account_path = _write_account_json(tmp_path, KEY_ACTIVE)
        monkeypatch.setattr(sc, "_opencode_account_json_paths", lambda: [account_path])
        auth = _write_auth(tmp_path, KEY_A)

        cards, blocked = sc.GenericCollector.collect_provider(
            "opencode",
            _file_config(auth),
            account_label_hints={
                "opencode": {f"path:{auth.resolve()}#{FP_A}": "alice@example.com"}
            },
        )

        assert blocked == []
        assert [c["account_id"] for c in cards] == ["alice@example.com"]

    def test_gate_applies_to_the_env_rule_too(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The gate is about the credential, not the rule type."""
        account_path = _write_account_json(tmp_path, KEY_ACTIVE)
        monkeypatch.setattr(sc, "_opencode_account_json_paths", lambda: [account_path])
        monkeypatch.setenv("OPENCODE_API_KEY", KEY_A)

        with caplog.at_level(logging.WARNING):
            cards, blocked = sc.GenericCollector.collect_provider(
                "opencode", _env_config(), account_label_hints=self._auto_hint()
            )

        assert cards == []
        assert blocked == [
            {"provider_id": "opencode", "credential_origin": f"env:OPENCODE_API_KEY#{FP_A}"}
        ]
        assert "provider-wide account hint withheld" in caplog.text


# ---------------------------------------------------------------------------
# _opencode_local_key_binding — the gate's classifier
# ---------------------------------------------------------------------------


@pytest.fixture()
def _state_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[Path]]:
    """Let a test install the ``account.json`` files the reader may return."""
    paths: list[Path] = []
    monkeypatch.setattr(sc, "_opencode_account_json_paths", lambda: list(paths))
    yield paths


class TestLocalKeyBinding:
    def test_no_state_is_unknown(self, _state_paths: list[Path]) -> None:
        assert sc._opencode_local_key_binding(KEY_A) == "unknown"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_no_key_is_unknown(self, _state_paths: list[Path], value: str | None) -> None:
        assert sc._opencode_local_key_binding(value) == "unknown"

    def test_matching_active_key_is_single(self, _state_paths: list[Path], tmp_path: Path) -> None:
        _state_paths.append(_write_account_json(tmp_path, KEY_A))
        assert sc._opencode_local_key_binding(KEY_A) == "single"

    def test_mismatched_active_key_is_ambiguous(
        self, _state_paths: list[Path], tmp_path: Path
    ) -> None:
        _state_paths.append(_write_account_json(tmp_path, KEY_ACTIVE))
        assert sc._opencode_local_key_binding(KEY_A) == "ambiguous"

    def test_state_is_paired_with_the_credentials_own_directory(
        self, _state_paths: list[Path], tmp_path: Path
    ) -> None:
        """Two install directories can hold one ``account.json`` apiece and
        disagree about which key is active. Only the one sitting next to the
        ``auth.json`` we actually read speaks for this credential — the other
        one neither outvotes it nor is outvoted by it."""
        auth = _write_auth(tmp_path / "install-a", KEY_A)
        own = _write_account_json(auth.resolve().parent, KEY_A)
        elsewhere = _write_account_json(tmp_path / "install-b", KEY_ACTIVE)
        _state_paths.extend([elsewhere, own])

        # Paired: the credential's own state agrees → safe to use the hint.
        assert (
            sc._opencode_local_key_binding(KEY_A, auth_origin=f"path:{auth.resolve()}") == "single"
        )
        # Unpaired (an env / keychain credential has no directory to pair
        # with): every known location is consulted, in the reader's order.
        assert sc._opencode_local_key_binding(KEY_A) == "ambiguous"

    def test_comparison_strips_surrounding_whitespace(
        self, _state_paths: list[Path], tmp_path: Path
    ) -> None:
        _state_paths.append(_write_account_json(tmp_path, f"  {KEY_A}\n"))
        assert sc._opencode_local_key_binding(f" {KEY_A} ") == "single"

    def test_unreadable_state_is_unknown(self, _state_paths: list[Path], tmp_path: Path) -> None:
        broken = tmp_path / "account.json"
        broken.write_text("{not json", encoding="utf-8")
        _state_paths.append(broken)
        assert sc._opencode_local_key_binding(KEY_A) == "unknown"

    @pytest.mark.parametrize(
        "state",
        [
            {"version": 2, "accounts": {}, "active": {}},
            {
                "version": 2,
                "accounts": {"a": {"id": "a", "credential": {"type": "api", "key": KEY_A}}},
                "active": {},
            },
            {
                "version": 2,
                "accounts": {"a": {"id": "a", "credential": {"type": "api"}}},
                "active": {"opencode-go": "a"},
            },
            {
                "version": 2,
                "accounts": {"a": {"id": "a", "credential": {"type": "api", "key": KEY_B}}},
                "active": {"ollama-cloud": "a"},
            },
            {
                "version": 2,
                "accounts": {"a": {"id": "a", "credential": {"type": "api", "key": KEY_A}}},
                "active": {"opencode-go": "missing-id"},
            },
            {"version": 2},
            [],
        ],
        ids=[
            "no-active-record",
            "empty-active",
            "active-without-key",
            "active-for-another-service",
            "active-id-not-in-accounts",
            "no-accounts-key",
            "not-a-dict",
        ],
    )
    def test_state_that_cannot_answer_is_unknown(
        self, _state_paths: list[Path], tmp_path: Path, state: Any
    ) -> None:
        path = tmp_path / "account.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        _state_paths.append(path)
        assert sc._opencode_local_key_binding(KEY_A) == "unknown"
