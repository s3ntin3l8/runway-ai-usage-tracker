"""Unit tests for the macOS Keychain reader for the Antigravity CLI credential.

The agy (Antigravity) CLI keeps its OAuth token in the macOS login Keychain
(service ``gemini``, account ``antigravity``) and writes no token file, so the
sidecar has to read the stored entry instead. The stored value is wrapped in
go-keyring's envelope, and the entry's access list trusts only the application
that wrote it - so the read goes through the Keychain worker, behind the
local consent the revamp added, and never through a credential requester.

These tests pin the pure logic - envelope decoding, the rule's mapping, the
consent gating and the keep-alive's due rules - so they run on every platform
and never touch the real Keychain.
"""

import base64
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import sidecar  # noqa: E402

from scripts.sidecar_pkg import source_consent as consent  # noqa: E402
from scripts.sidecar_pkg import source_setup  # noqa: E402
from scripts.sidecar_pkg.keyring_value import (  # noqa: E402
    BASE64_PREFIX,
    HEX_PREFIX,
    decode_document,
    decode_envelope,
)
from scripts.sidecar_pkg.source_catalog import catalog, trusted_rule  # noqa: E402

_SERVICE = "gemini"
_ACCOUNT = "antigravity"
_SOURCE_ID = "antigravity.agy-cli"
# The origin both platforms use for this credential, so the server files the
# same account under one key whichever store it came from.
_ORIGIN = f"keychain:{_SERVICE}:{_ACCOUNT}"

# The shape the agy CLI really stores: the token bundle nested under "token",
# plus the id_token the CLI keeps alongside it. The expiry is stamped per
# test - a fixed one would make the suite go stale.
_AGY_DOCUMENT = {
    "token": {
        "access_token": "ya29.fake-access-token",
        "token_type": "Bearer",
        "refresh_token": "1//fake-refresh-token",
        "expires_in": 3600,
    },
    "auth_method": "consumer",
    "id_token": "fake-id-token",
}


def _document(expires_at: datetime) -> dict:
    """The stored document with its access token expiring at *expires_at*."""
    document = json.loads(json.dumps(_AGY_DOCUMENT))
    document["token"]["expiry"] = expires_at.isoformat()
    return document


def _in_an_hour() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def _an_hour_ago() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


def _envelope(document: dict) -> bytes:
    """The stored value: go-keyring's base64 envelope around *document*."""
    return (BASE64_PREFIX + base64.b64encode(json.dumps(document).encode()).decode()).encode()


def _keychain_rule() -> dict:
    """The production rule for the macOS Keychain entry."""
    return next(
        rule
        for rule in sidecar.__REGISTRY__["providers"]["antigravity"]["rules"]
        if rule["type"] == "keychain" and rule.get("service_name") == _SERVICE
    )


def _provider_config() -> dict:
    """The real production provider spec (not a hand-rolled subset)."""
    config = json.loads(json.dumps(sidecar.__REGISTRY__["providers"]["antigravity"]))
    config["rules"] = [rule for rule in config["rules"] if rule["type"] == "keychain"]
    return config


def _granted_config(**overrides) -> dict:
    """An app config with the local consent this source needs."""
    config = {"api_url": "https://runway.example", "api_key": "synthetic-private"}
    config.update(overrides)
    return consent.SourcePolicy.change(config, _SOURCE_ID, read=True)


@pytest.fixture(autouse=True)
def _restore_policy():
    """The consent policy is global; no grant may leak into another test."""
    yield
    consent.POLICY.configure({})


class TestKeyringEnvelope:
    """The envelope go-keyring writes the credential in."""

    def test_a_base64_envelope_decodes_to_its_plaintext(self):
        document = _document(_in_an_hour())
        raw = BASE64_PREFIX + base64.b64encode(json.dumps(document).encode()).decode()
        assert json.loads(decode_envelope(raw)) == document

    def test_a_hex_envelope_decodes_to_its_plaintext(self):
        document = _document(_in_an_hour())
        raw = HEX_PREFIX + json.dumps(document).encode().hex()
        assert json.loads(decode_envelope(raw)) == document

    def test_a_value_without_an_envelope_is_returned_unchanged(self):
        """go-keyring keeps plain strings as they are, and a future writer that
        stops wrapping the secret must still read."""
        assert decode_envelope("plain-value") == "plain-value"

    def test_a_corrupt_base64_payload_decodes_to_nothing(self):
        assert decode_envelope(BASE64_PREFIX + "not base64!!") is None

    def test_a_corrupt_hex_payload_decodes_to_nothing(self):
        assert decode_envelope(HEX_PREFIX + "nothex") is None

    def test_a_payload_that_is_not_utf8_decodes_to_nothing(self):
        raw = BASE64_PREFIX + base64.b64encode(b"\xff\xfe").decode()
        assert decode_envelope(raw) is None

    def test_a_document_is_the_parsed_object(self):
        document = _document(_in_an_hour())
        raw = BASE64_PREFIX + base64.b64encode(json.dumps(document).encode()).decode()
        assert decode_document(raw) == document

    def test_a_json_array_is_not_a_document(self):
        raw = BASE64_PREFIX + base64.b64encode(b"[1, 2]").decode()
        assert decode_document(raw) is None

    def test_text_that_is_not_json_is_not_a_document(self):
        raw = BASE64_PREFIX + base64.b64encode(b"not json").decode()
        assert decode_document(raw) is None


class TestMacosKeychainRule:
    """The rule that maps the Keychain entry onto token fields."""

    @staticmethod
    def _collect(stored: bytes | None, *, config: dict | None = None):
        consent.POLICY.configure(config if config is not None else _granted_config())
        with (
            patch.object(sidecar.platform, "system", return_value="Darwin"),
            patch(
                "scripts.sidecar_pkg.macos_keychain_store.read_secret",
                return_value=stored,
            ) as read,
        ):
            cards, _blocked = sidecar.GenericCollector.collect_provider(
                "antigravity", _provider_config()
            )
        return cards, read

    def test_the_rule_targets_the_agy_keychain_entry(self):
        rule = _keychain_rule()
        assert rule["service_name"] == _SERVICE
        assert rule["account"] == _ACCOUNT

    def test_the_rule_maps_the_same_three_fields_as_the_file_rule(self):
        """The Keychain entry holds the same bundle as the POSIX token file, so
        both rules must map the same fields - otherwise the two platforms would
        push different credential shapes."""
        rules = sidecar.__REGISTRY__["providers"]["antigravity"]["rules"]
        file_rule = next(rule for rule in rules if rule["type"] == "file")
        assert _keychain_rule()["mapping"] == file_rule["mapping"]

    def test_the_id_token_is_not_mapped(self):
        """The stored bundle carries an id_token the POSIX file does not.
        Mapping it on one platform only would file the same Google account
        under two different cache keys, so it stays unmapped everywhere."""
        assert "id_token" not in _keychain_rule()["mapping"]
        assert not any("id_token" in target for target in _keychain_rule()["mapping"].values())

    def test_the_entry_is_read_with_its_service_and_account(self):
        """Service 'gemini' holds two entries - the CLI's own and the Antigravity
        ACP server's - so the account is what makes the selection exact."""
        _cards, read = self._collect(_envelope(_document(_in_an_hour())))
        read.assert_called_once()
        args, kwargs = read.call_args
        assert args[0] == _SERVICE
        assert args[1] == _ACCOUNT
        assert "group" in kwargs

    def test_the_token_fields_are_extracted(self):
        cards, _read = self._collect(_envelope(_document(_in_an_hour())))
        token_cards = [card for card in cards if card.get("remaining") == "Token"]
        assert len(token_cards) == 1
        meta = token_cards[0]["metadata"]
        assert meta["oauth_token"] == _AGY_DOCUMENT["token"]["access_token"]
        assert meta["refresh_token"] == _AGY_DOCUMENT["token"]["refresh_token"]

    def test_the_expiry_is_stamped_as_ms_epoch(self):
        """agy's access token carries no JWT exp claim, so expiry_date (the
        freshness signal token_cache._is_staler needs) comes from token.expiry."""
        expires_at = _in_an_hour()
        cards, _read = self._collect(_envelope(_document(expires_at)))
        token_cards = [card for card in cards if card.get("remaining") == "Token"]
        assert len(token_cards) == 1
        meta = token_cards[0]["metadata"]
        assert int(meta["expiry_date"]) == int(expires_at.timestamp() * 1000)
        assert "_raw_expiry" not in meta  # temp field must not leak into the push

    def test_an_expired_token_is_not_pushed(self):
        """A lapsed agy session must not push its dead credential: agy owns its
        own refresh, so pushing it would re-poison the shared server cache."""
        cards, _read = self._collect(_envelope(_document(_an_hour_ago())))
        assert [card for card in cards if card.get("remaining") == "Token"] == []

    def test_a_missing_credential_yields_no_card(self):
        cards, _read = self._collect(None)
        assert [card for card in cards if card.get("remaining") == "Token"] == []

    def test_a_corrupt_envelope_yields_no_card(self):
        """An unreadable envelope must never produce a credential: the reader
        has no way to tell a truncated payload from a valid one."""
        cards, _read = self._collect((BASE64_PREFIX + "not base64!!").encode())
        assert [card for card in cards if card.get("remaining") == "Token"] == []

    def test_a_corrupt_envelope_does_not_re_push_the_file_rules_tokens(self, tmp_path):
        """A rule that finds nothing must not carry the previous rule's
        candidate into its own origin.

        The file rule runs first and populates the candidate; the Keychain
        rule then finds an unreadable envelope. Without a per-rule reset the
        file rule's token would be pushed a second time under the Keychain
        origin - the same credential filed twice.
        """
        token_file = tmp_path / "antigravity-oauth-token"
        token_file.write_text(json.dumps(_document(_in_an_hour())))
        config = json.loads(json.dumps(sidecar.__REGISTRY__["providers"]["antigravity"]))
        for rule in config["rules"]:
            if rule["type"] == "file":
                rule["paths"] = [str(token_file)]
        consent.POLICY.configure(_granted_config())
        with (
            patch.object(sidecar.platform, "system", return_value="Darwin"),
            patch(
                "scripts.sidecar_pkg.macos_keychain_store.read_secret",
                return_value=(BASE64_PREFIX + "not base64!!").encode(),
            ),
        ):
            cards, _blocked = sidecar.GenericCollector.collect_provider("antigravity", config)
        origins = [
            card["metadata"]["credential_origin"]
            for card in cards
            if card.get("remaining") == "Token"
        ]
        assert len(origins) == 1
        assert origins[0].startswith("path:")
        assert not any(origin.startswith("keychain:") for origin in origins)

    def test_the_origin_names_the_credential_store_entry(self):
        """The origin identifies the source. It carries the ``keychain:`` prefix
        (the store is a per-machine secret store, so the origin is machine-bound
        and cannot take a deployment-wide tag) and names the entry exactly."""
        cards, _read = self._collect(_envelope(_document(_in_an_hour())))
        token_cards = [card for card in cards if card.get("remaining") == "Token"]
        assert len(token_cards) == 1
        assert token_cards[0]["metadata"]["credential_origin"] == _ORIGIN

    def test_the_rule_is_skipped_off_macos(self):
        """The same baked registry ships to every platform; the rule must not
        fire where there is no Keychain."""
        consent.POLICY.configure(_granted_config())
        with (
            patch.object(sidecar.platform, "system", return_value="Linux"),
            patch("scripts.sidecar_pkg.macos_keychain_store.read_secret") as read,
        ):
            sidecar.GenericCollector.collect_provider("antigravity", _provider_config())
        read.assert_not_called()

    def test_without_local_consent_nothing_is_pushed(self):
        """The source is off until the user grants it: the worker refuses the
        read (see TestWorkerConsentGate) and no credential is pushed."""
        cards, _read = self._collect(_envelope(_document(_in_an_hour())), config={})
        assert [card for card in cards if card.get("remaining") == "Token"] == []

    def test_a_stale_revision_is_refused(self):
        """A grant made against an older catalog revision must not carry over:
        the rule it authorized is not the rule that runs now."""
        config = _granted_config()
        config["keychain_sources"][_SOURCE_ID]["revision"] = "0" * 64
        cards, _read = self._collect(_envelope(_document(_in_an_hour())), config=config)
        assert [card for card in cards if card.get("remaining") == "Token"] == []


class TestConsentGating:
    """The gate decisions, without touching the worker.

    The worker itself refuses a read that has no consent ticket
    (``macos_keychain_store._WorkerAPI.call``), but unit tests may not run it
    - the suite forbids native Keychain IO. What is pinned here is the
    decision around it: no grant, no ticket, no credential.
    """

    def test_without_a_grant_there_is_no_ticket(self):
        consent.POLICY.configure({})
        assert consent.POLICY.ticket(_SOURCE_ID) is None

    def test_a_grant_against_the_current_revision_yields_a_ticket(self):
        consent.POLICY.configure(_granted_config())
        ticket = consent.POLICY.ticket(_SOURCE_ID)
        assert ticket is not None
        assert ticket.source_id == _SOURCE_ID
        assert consent.POLICY.current(ticket) is True

    def test_a_grant_against_another_revision_yields_no_ticket(self):
        config = _granted_config()
        config["keychain_sources"][_SOURCE_ID]["revision"] = "0" * 64
        consent.POLICY.configure(config)
        assert consent.POLICY.ticket(_SOURCE_ID) is None

    def test_a_grant_for_another_destination_yields_no_ticket(self):
        """A grant is bound to the server it was given to."""
        config = _granted_config()
        config["keychain_sources"][_SOURCE_ID]["destination"] = "https://other.example"
        consent.POLICY.configure(config)
        assert consent.POLICY.ticket(_SOURCE_ID) is None


class TestCatalogAndConsent:
    """The catalog entry, the consent mapping and the authorize flow."""

    def test_the_source_is_catalogued(self):
        descriptor = catalog()[_SOURCE_ID]
        assert descriptor.services == (_SERVICE,)
        assert descriptor.account == _ACCOUNT
        assert descriptor.renewal is False

    def test_the_catalogued_rule_is_the_one_that_runs(self):
        assert trusted_rule("antigravity", _keychain_rule())

    def test_the_descriptor_account_matches_the_rule(self):
        """The authorize flow and the read must agree on the entry: the
        descriptor takes its account from the rule, so they cannot drift."""
        assert catalog()[_SOURCE_ID].account == _keychain_rule()["account"]

    def test_the_account_is_covered_by_the_revision(self, monkeypatch):
        """The account identifies the entry, so a rule that repoints it has to
        invalidate the consent that was granted for the old one."""
        from scripts.sidecar_pkg import source_catalog

        before = catalog()[_SOURCE_ID].revision
        rules = json.loads(json.dumps(source_catalog.KEYCHAIN_RULES))
        for rule in rules["antigravity"]["rules"]:
            if rule["type"] == "keychain":
                rule["account"] = "some-other-account"
        monkeypatch.setattr(source_catalog, "KEYCHAIN_RULES", rules)
        changed = catalog()[_SOURCE_ID]
        assert changed.account == "some-other-account"
        assert changed.revision != before

    def test_the_source_belongs_to_the_antigravity_provider_only(self):
        assert not [key for key in catalog() if key.endswith(".agy-cli") and key != _SOURCE_ID]

    def test_the_origin_maps_to_the_source(self):
        assert consent.source_for_origin("antigravity", _ORIGIN) == _SOURCE_ID

    def test_the_acp_entry_is_not_our_source(self):
        """Service 'gemini' also holds the Antigravity ACP server's own token;
        that entry is not ours to read."""
        assert consent.source_for_origin("antigravity", f"keychain:{_SERVICE}:antigravity-acp") == (
            "unknown"
        )

    def test_the_service_resolves_to_a_ticket_inside_the_rule_scope(self):
        consent.POLICY.configure(_granted_config())
        with consent.rule_scope("antigravity", _keychain_rule()):
            ticket = consent.service_ticket(_SERVICE)
        assert ticket is not None
        assert ticket.source_id == _SOURCE_ID

    def test_the_service_resolves_to_no_ticket_outside_a_rule_scope(self):
        consent.POLICY.configure(_granted_config())
        assert consent.service_ticket(_SERVICE) is None

    def test_another_providers_keychain_rule_gets_no_ticket(self):
        """A shared OS grant must not enable another scraper."""
        consent.POLICY.configure(_granted_config())
        rule = {"type": "keychain", "service_name": _SERVICE, "account": _ACCOUNT}
        with consent.rule_scope("github", rule):
            assert consent.service_ticket(_SERVICE) is None

    def test_the_authorize_flow_selects_the_exact_entry(self):
        """The service holds two entries; the account is what makes the
        foreground authorization pick one."""
        assert source_setup.resources(_SOURCE_ID) == ([_SERVICE], _ACCOUNT)

    def test_the_keep_alive_reads_the_catalogued_rule(self):
        """The keep-alive must use the rule the consent map was built from, or
        the worker would refuse every read it attempts."""
        from scripts.sidecar_pkg import keep_alive

        rule = keep_alive._macos_keychain_rule()
        assert rule == _keychain_rule()
        assert trusted_rule("antigravity", rule)

    def test_the_keep_alive_matches_the_account_too(self, monkeypatch):
        """A second rule for the same service must not be picked up by
        accident, so the lookup matches service *and* account."""
        from scripts.sidecar_pkg import keep_alive, source_catalog

        rules = json.loads(json.dumps(source_catalog.KEYCHAIN_RULES))
        for rule in rules["antigravity"]["rules"]:
            if rule["type"] == "keychain":
                rule["account"] = "some-other-account"
        monkeypatch.setattr(source_catalog, "KEYCHAIN_RULES", rules)
        assert keep_alive._macos_keychain_rule() is None

    def test_the_source_is_off_until_it_is_granted(self):
        from scripts.sidecar_pkg.source_settings import report

        sources = {source["id"]: source for source in report({})["sources"]}
        assert _SOURCE_ID in sources
        assert sources[_SOURCE_ID]["read"] is False
        assert sources[_SOURCE_ID]["dependency"] == f"Keychain: {_SERVICE}"

    def test_the_credential_is_withheld_from_egress(self, monkeypatch):
        """A Keychain-backed credential stays on the host while the withdrawal
        lifecycle is missing - consent grants the local read, not the egress."""
        from scripts.sidecar_pkg import source_consent

        monkeypatch.setattr(source_consent.sys, "platform", "darwin")
        filtered = source_consent.filter_egress(
            {
                "metrics": [
                    {
                        "metadata": {
                            "provider_id": "antigravity",
                            "credential_origin": _ORIGIN,
                        }
                    }
                ],
                "entries": [],
                "observations": [],
                "completed_providers": ["antigravity"],
            }
        )
        assert filtered["metrics"] == []
        # Withholding a source is not proof of logout, so the provider stays
        # out of the completed list and the server keeps its last-good rows.
        assert "antigravity" not in filtered["completed_providers"]


class TestKeepAlive:
    """The keep-alive reads the same entry, and only with consent.

    Every test here runs against a token path of its own and a pinned CLI
    version: the machine's real agy login must not decide whether a test
    passes.
    """

    @pytest.fixture(autouse=True)
    def _hermetic(self, tmp_path, monkeypatch):
        from scripts.sidecar_pkg import agy_version, keep_alive

        # No token file, and a CLI that uses the Keychain - the 1.4.3+ layout.
        # The version is pinned rather than the predicate, so the real
        # "is the file still the credential" logic is what runs.
        monkeypatch.setattr(keep_alive, "DEFAULT_TOKEN_PATH", tmp_path / "antigravity-oauth-token")
        monkeypatch.setattr(agy_version, "installed_version", lambda: "1.4.3")
        self._token_file = tmp_path / "antigravity-oauth-token"

    @staticmethod
    def _uses_the_file(monkeypatch, version: str = "1.4.2") -> None:
        """Pin a CLI that still keeps its credential in the token file."""
        from scripts.sidecar_pkg import agy_version

        monkeypatch.setattr(agy_version, "installed_version", lambda: version)

    def _write_file(self, document: dict) -> None:
        self._token_file.write_text(json.dumps(document))

    @staticmethod
    def _due(stored: bytes | None, *, config: dict | None = None) -> bool:
        from scripts.sidecar_pkg import keep_alive

        consent.POLICY.configure(config if config is not None else _granted_config())
        with (
            patch.object(keep_alive.platform, "system", return_value="Darwin"),
            patch(
                "scripts.sidecar_pkg.macos_keychain_store.read_secret",
                return_value=stored,
            ),
        ):
            return keep_alive.local_refresh_due()

    def test_an_absent_entry_is_not_due(self):
        """A machine that never logged into agy must not be prodded into
        starting a login flow."""
        assert self._due(None) is False

    def test_without_local_consent_the_keep_alive_stays_quiet(self):
        """Without a grant the keep-alive does not even ask for the entry."""
        from scripts.sidecar_pkg import keep_alive

        consent.POLICY.configure({})
        with (
            patch.object(keep_alive.platform, "system", return_value="Darwin"),
            patch("scripts.sidecar_pkg.macos_keychain_store.read_secret") as read,
        ):
            assert keep_alive.local_refresh_due() is False
        read.assert_not_called()

    def test_a_live_token_is_not_due(self):
        assert self._due(_envelope(_document(_in_an_hour()))) is False

    def test_a_lapsed_token_is_due(self):
        assert self._due(_envelope(_document(_an_hour_ago()))) is True

    def test_an_unreadable_expiry_is_due(self):
        """Let the CLI decide: an expiry we cannot read must not keep the
        keep-alive from trying."""
        document = _document(_in_an_hour())
        document["token"]["expiry"] = "not-a-timestamp"
        assert self._due(_envelope(document)) is True

    def test_a_corrupt_envelope_is_due(self):
        assert self._due((BASE64_PREFIX + "not base64!!").encode()) is True

    def test_the_expiry_comes_from_the_keychain_entry(self):
        from scripts.sidecar_pkg import keep_alive

        expires_at = _in_an_hour()
        consent.POLICY.configure(_granted_config())
        with (
            patch.object(keep_alive.platform, "system", return_value="Darwin"),
            patch(
                "scripts.sidecar_pkg.macos_keychain_store.read_secret",
                return_value=_envelope(_document(expires_at)),
            ),
        ):
            expiry = keep_alive.local_expiry_epoch()
        assert expiry == pytest.approx(expires_at.timestamp())

    def test_a_lapsed_file_token_is_still_due_on_macos(self, monkeypatch):
        """agy before 1.4.3 keeps its credential in the token file, and that
        layout needs no consent: the keep-alive must not go quiet on it."""
        from scripts.sidecar_pkg import keep_alive

        self._write_file(_document(_an_hour_ago()))
        self._uses_the_file(monkeypatch)
        consent.POLICY.configure(_granted_config())
        with (
            patch.object(keep_alive.platform, "system", return_value="Darwin"),
            patch("scripts.sidecar_pkg.macos_keychain_store.read_secret", return_value=None),
        ):
            assert keep_alive.local_refresh_due() is True

    def test_a_file_login_needs_no_consent(self, monkeypatch):
        """The file layout is not consent-gated - the file is not the Keychain."""
        from scripts.sidecar_pkg import keep_alive

        self._write_file(_document(_an_hour_ago()))
        self._uses_the_file(monkeypatch)
        consent.POLICY.configure({})
        with (
            patch.object(keep_alive.platform, "system", return_value="Darwin"),
            patch("scripts.sidecar_pkg.macos_keychain_store.read_secret", return_value=None),
        ):
            assert keep_alive.local_refresh_due() is True

    def test_a_leftover_file_is_not_the_credential_on_a_newer_cli(self, monkeypatch):
        """A 1.4.3+ install leaves the old file behind, and the renewal writes
        the Keychain - never the file. Reading that stale file would say "due"
        forever and respawn ``agy models`` every tick."""
        from scripts.sidecar_pkg import keep_alive

        self._write_file(_document(_an_hour_ago()))
        consent.POLICY.configure({})
        with (
            patch.object(keep_alive.platform, "system", return_value="Darwin"),
            patch("scripts.sidecar_pkg.macos_keychain_store.read_secret", return_value=None),
        ):
            assert keep_alive.local_refresh_due() is False

    def test_a_live_file_token_is_not_due_on_macos(self, monkeypatch):
        from scripts.sidecar_pkg import keep_alive

        self._write_file(_document(_in_an_hour()))
        self._uses_the_file(monkeypatch)
        consent.POLICY.configure(_granted_config())
        with (
            patch.object(keep_alive.platform, "system", return_value="Darwin"),
            patch("scripts.sidecar_pkg.macos_keychain_store.read_secret", return_value=None),
        ):
            assert keep_alive.local_refresh_due() is False

    def test_no_keychain_entry_and_no_file_is_not_due(self):
        from scripts.sidecar_pkg import keep_alive

        consent.POLICY.configure(_granted_config())
        with (
            patch.object(keep_alive.platform, "system", return_value="Darwin"),
            patch("scripts.sidecar_pkg.macos_keychain_store.read_secret", return_value=None),
        ):
            assert keep_alive.local_refresh_due() is False

    @pytest.mark.parametrize(
        ("version", "uses_the_file"),
        [
            ("1.4.2", True),
            ("1.4.3", False),
            ("1.5.0", False),
            ("2.0", False),
            ("1.4", True),
            (None, False),
            ("not-a-version", False),
        ],
    )
    def test_only_a_cli_that_still_writes_the_file_makes_it_authoritative(
        self, monkeypatch, version, uses_the_file
    ):
        from scripts.sidecar_pkg import agy_version, keep_alive

        monkeypatch.setattr(agy_version, "installed_version", lambda: version)
        assert keep_alive._installed_agy_uses_the_file() is uses_the_file

    def test_the_file_layout_still_decides_on_posix(self):
        """The macOS branch must not change the POSIX file behaviour."""
        from scripts.sidecar_pkg import keep_alive

        with patch.object(keep_alive.platform, "system", return_value="Linux"):
            assert keep_alive.local_refresh_due(Path("/nonexistent/token")) is False
