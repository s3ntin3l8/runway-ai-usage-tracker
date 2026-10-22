"""Unit tests for the Windows Credential Manager reader and its rule.

The agy (Antigravity) CLI keeps its OAuth token in the Windows Credential
Manager under the target ``gemini:antigravity`` and writes no token file, so
the sidecar has to read the stored entry. The reader is native (advapi32):
the earlier PowerShell route needed the CredentialManager PowerShell module,
which is not part of Windows, so every lookup silently returned nothing and
the rule was dead on the one platform it was written for.

These tests pin the parts that are pure logic (blob decoding, the rule's
mapping and expiry handling) so they run identically on every platform; the
live Win32 call is exercised on Windows only.
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
import sidecar  # noqa: E402

from scripts.sidecar_pkg import wincred  # noqa: E402

# The real agy blob shape: a JSON document with the token bundle nested
# under "token", plus the id_token the CLI stores alongside it. The expiry is
# stamped per test - a fixed one would make the suite go stale.
_AGY_BLOB = {
    "token": {
        "access_token": "ya29.fake-access-token",
        "token_type": "Bearer",
        "refresh_token": "1//fake-refresh-token",
        "expires_in": 3600,
    },
    "auth_method": "consumer",
    "id_token": "fake-id-token",
}

_TARGET = "gemini:antigravity"


def _blob(expires_at: datetime) -> dict:
    """The agy blob with its access token expiring at *expires_at*."""
    blob = json.loads(json.dumps(_AGY_BLOB))
    blob["token"]["expiry"] = expires_at.isoformat()
    return blob


def _in_an_hour() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def _an_hour_ago() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


class TestDecodeCredential:
    """A credential blob is raw bytes; the encoding is the writer's choice."""

    def test_utf8_json_blob_decodes_as_utf8(self):
        raw = json.dumps(_AGY_BLOB).encode("utf-8")
        assert json.loads(wincred.decode_credential(raw)) == _AGY_BLOB

    def test_utf16le_blob_decodes_as_utf16le(self):
        """The GitHub CLI writes its token as UTF-16LE."""
        raw = "fake-github-token".encode("utf-16-le")
        assert wincred.decode_credential(raw) == "fake-github-token"

    def test_utf16_bom_is_honoured(self):
        raw = b"\xff\xfe" + "hello".encode("utf-16-le")
        assert wincred.decode_credential(raw) == "hello"

    def test_empty_blob_decodes_to_empty_text(self):
        assert wincred.decode_credential(b"") == ""

    def test_single_byte_blob_is_not_mistaken_for_utf16(self):
        """A one-byte blob has no odd byte to read, so it must stay UTF-8."""
        assert wincred.decode_credential(b"x") == "x"

    def test_multibyte_utf8_is_not_mistaken_for_utf16(self):
        """UTF-8 text with a two-byte character has a non-NUL odd byte."""
        assert wincred.decode_credential("é".encode()) == "é"

    def test_a_truncated_utf16_blob_does_not_raise(self):
        """A corrupt blob must not abort a collection cycle.

        The BOM branch used a strict decode, so an odd-length UTF-16 blob
        raised UnicodeDecodeError out of the rule branch and took the whole
        antigravity cycle with it.
        """
        assert isinstance(wincred.decode_credential(b"\xff\xfe" + b"a"), str)

    def test_decoding_never_raises(self):
        """Whatever the store holds, decoding returns text."""
        for raw in (b"", b"\x00", b"\xff", b"\xfe", b"\xff\xfe", b"\xff\xfe\x00", b"\x00\x00"):
            assert isinstance(wincred.decode_credential(raw), str)


class TestCorruptBlobDoesNotAbortACycle:
    """One corrupt store entry must not take a whole provider cycle down.

    The BOM branch used a strict decode, so a truncated UTF-16 blob raised
    UnicodeDecodeError out of the reader and out of ``collect_provider``.
    """

    def test_the_flat_reader_returns_nothing_for_a_corrupt_blob(self):
        with patch.object(wincred, "read_credential_bytes", return_value=b"\xff\xfe" + b"a"):
            assert wincred.read_credential_text(_TARGET) is None

    def test_the_flat_credential_lookup_does_not_raise(self):
        with (
            patch.object(sidecar.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_credential_bytes", return_value=b"\xff\xfe" + b"a"
            ),
        ):
            assert sidecar.get_windows_credential("github.com") is None

    def test_the_provider_cycle_completes(self):
        config = json.loads(json.dumps(sidecar.__REGISTRY__["providers"]["antigravity"]))
        config["rules"] = [r for r in config["rules"] if r["type"] == "windows_credential_json"]
        with (
            patch.object(sidecar.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_credential_bytes", return_value=b"\xff\xfe" + b"a"
            ),
        ):
            cards, _blocked = sidecar.GenericCollector.collect_provider("antigravity", config)
        assert [c for c in cards if c.get("remaining") == "Token"] == []


class TestReadJsonCredential:
    def test_absent_target_returns_none(self):
        with patch.object(wincred, "read_credential_bytes", return_value=None):
            assert wincred.read_json_credential(_TARGET) is None

    def test_a_corrupt_blob_reads_as_absent(self):
        """A blob that does not decode cleanly holds no usable credential."""
        with patch.object(wincred, "read_credential_bytes", return_value=b"\xff\xfe" + b"a"):
            assert wincred.read_json_credential(_TARGET) is None

    def test_non_json_blob_returns_none(self):
        with patch.object(wincred, "read_credential_bytes", return_value=b"not json"):
            assert wincred.read_json_credential(_TARGET) is None

    def test_json_array_blob_returns_none(self):
        """Only a JSON object is a credential document."""
        with patch.object(wincred, "read_credential_bytes", return_value=b"[1, 2]"):
            assert wincred.read_json_credential(_TARGET) is None

    def test_json_object_is_returned(self):
        raw = json.dumps(_AGY_BLOB).encode()
        with patch.object(wincred, "read_credential_bytes", return_value=raw):
            assert wincred.read_json_credential(_TARGET) == _AGY_BLOB

    def test_absent_target_reads_nothing_on_other_platforms(self):
        """Off Windows there is no Credential Manager to ask."""
        with patch.object(wincred.platform, "system", return_value="Linux"):
            assert wincred.read_credential_bytes(_TARGET) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
class TestLiveCredentialManager:
    """The native call against the real store, on a Windows box.

    A target that certainly does not exist must read as absent rather than
    raise — that is what keeps a missing credential from breaking a cycle.
    """

    def test_missing_target_reads_as_absent(self):
        assert wincred.read_credential_bytes("runway-test-target-does-not-exist") is None

    def test_missing_target_text_is_none(self):
        assert wincred.read_credential_text("runway-test-target-does-not-exist") is None


class TestWindowsCredentialJsonRule:
    """The rule that maps a Credential Manager entry onto token fields."""

    @staticmethod
    def _config() -> dict:
        """The real production rule (not a hand-rolled subset)."""
        config = json.loads(json.dumps(sidecar.__REGISTRY__["providers"]["antigravity"]))
        config["rules"] = [r for r in config["rules"] if r["type"] == "windows_credential_json"]
        return config

    def _collect(self, blob: dict | None):
        with (
            patch.object(sidecar.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_json_credential",
                return_value=blob,
            ),
        ):
            return sidecar.GenericCollector.collect_provider("antigravity", self._config())

    def test_the_rule_targets_the_agy_credential_entry(self):
        rule = next(
            r
            for r in sidecar.__REGISTRY__["providers"]["antigravity"]["rules"]
            if r["type"] == "windows_credential_json"
        )
        assert rule["target"] == _TARGET

    def test_the_rule_maps_the_same_three_fields_as_the_file_rule(self):
        """The Windows entry holds the same bundle as the POSIX token file, so
        both rules must map the same fields — otherwise the two platforms
        would push different credential shapes."""
        rules = sidecar.__REGISTRY__["providers"]["antigravity"]["rules"]
        file_rule = next(r for r in rules if r["type"] == "file")
        wincred_rule = next(r for r in rules if r["type"] == "windows_credential_json")
        assert wincred_rule["mapping"] == file_rule["mapping"]

    def test_the_token_fields_are_extracted(self):
        cards, _blocked = self._collect(_blob(_in_an_hour()))
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1
        meta = token_cards[0]["metadata"]
        assert meta["oauth_token"] == _AGY_BLOB["token"]["access_token"]
        assert meta["refresh_token"] == _AGY_BLOB["token"]["refresh_token"]

    def test_the_expiry_is_stamped_as_ms_epoch(self):
        """agy's access token carries no JWT exp claim, so expiry_date (the
        freshness signal token_cache._is_staler needs) comes from token.expiry."""
        expires_at = _in_an_hour()
        cards, _blocked = self._collect(_blob(expires_at))
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1
        meta = token_cards[0]["metadata"]
        assert int(meta["expiry_date"]) == int(expires_at.timestamp() * 1000)
        assert "_raw_expiry" not in meta  # temp field must not leak into the push

    def test_an_expired_token_is_not_pushed(self):
        """A lapsed agy session must not push its dead credential: agy owns its
        own refresh, so pushing it would re-poison the shared server cache."""
        cards, _blocked = self._collect(_blob(_an_hour_ago()))
        assert [c for c in cards if c.get("remaining") == "Token"] == []

    def test_a_missing_credential_yields_no_card(self):
        cards, _blocked = self._collect(None)
        assert [c for c in cards if c.get("remaining") == "Token"] == []

    def test_the_origin_names_the_credential_store_entry(self):
        """The origin identifies the source. It carries the ``keychain:`` prefix
        (the store is a per-machine secret store, so the origin is machine-bound
        and cannot take a deployment-wide tag) and the target as its detail."""
        cards, _blocked = self._collect(_blob(_in_an_hour()))
        token_cards = [c for c in cards if c.get("remaining") == "Token"]
        assert len(token_cards) == 1
        origin = token_cards[0]["metadata"]["credential_origin"]
        assert origin == f"keychain:{_TARGET}"

    def test_the_rule_is_skipped_off_windows(self):
        """The same baked registry ships to every platform; the rule must not
        fire where there is no Credential Manager."""
        with (
            patch.object(sidecar.platform, "system", return_value="Linux"),
            patch("scripts.sidecar_pkg.wincred.read_json_credential") as read,
        ):
            cards, _blocked = sidecar.GenericCollector.collect_provider(
                "antigravity", self._config()
            )
        read.assert_not_called()
        assert [c for c in cards if c.get("remaining") == "Token"] == []

    def test_the_windows_and_posix_rules_agree(self):
        """Both platforms must push the same credential shape, so the server
        resolves the account the same way whichever sidecar reported it."""
        blob = _blob(_in_an_hour())

        win_cards, _ = self._collect(blob)
        win = next(c for c in win_cards if c.get("remaining") == "Token")

        tmp = Path(__import__("tempfile").mkdtemp())
        tok = tmp / "antigravity-oauth-token"
        tok.write_text(json.dumps(blob))
        config = json.loads(json.dumps(sidecar.__REGISTRY__["providers"]["antigravity"]))
        config["rules"] = [r for r in config["rules"] if r["type"] == "file"]
        config["rules"][0]["paths"] = [str(tok)]
        posix_cards, _ = sidecar.GenericCollector.collect_provider("antigravity", config)
        posix = next(c for c in posix_cards if c.get("remaining") == "Token")

        def shape(card):
            meta = card["metadata"]
            return {k: v for k, v in meta.items() if k not in ("credential_origin", "provider_id")}

        assert shape(win) == shape(posix)


class TestKeepAliveWindowsSource:
    """The keep-alive must renew the token wherever this OS keeps it."""

    def test_the_windows_target_is_the_agy_entry(self):
        from scripts.sidecar_pkg import keep_alive

        assert keep_alive.WINDOWS_CREDENTIAL_TARGET == _TARGET

    def test_a_windows_credential_is_due_once_it_has_lapsed(self):
        """A token that has actually lapsed must be due.

        The blob carries a real ``token.expiry`` in the past, so this
        exercises the lapsed branch - not the unreadable-expiry branch,
        which is due by default and would pass for the wrong reason.
        """
        from scripts.sidecar_pkg import keep_alive

        raw = json.dumps(_blob(_an_hour_ago())).encode()
        with (
            patch.object(keep_alive.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_credential_bytes",
                return_value=raw,
            ),
        ):
            assert keep_alive.local_refresh_due() is True

    def test_a_windows_credential_with_an_unreadable_expiry_is_due(self):
        """An entry whose expiry cannot be read counts as due - let the CLI decide."""
        from scripts.sidecar_pkg import keep_alive

        with (
            patch.object(keep_alive.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_credential_bytes",
                return_value=b"{}",
            ),
        ):
            assert keep_alive.local_refresh_due() is True

    def test_a_machine_without_an_agy_credential_is_not_prodded(self):
        """A machine that never logged into agy must not be pushed into
        starting a login flow."""
        from scripts.sidecar_pkg import keep_alive

        with (
            patch.object(keep_alive.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_credential_bytes",
                return_value=None,
            ),
        ):
            assert keep_alive.local_refresh_due() is False

    def test_a_fresh_windows_credential_is_not_due(self):
        from scripts.sidecar_pkg import keep_alive

        raw = json.dumps(_blob(_in_an_hour())).encode()
        with (
            patch.object(keep_alive.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_credential_bytes",
                return_value=raw,
            ),
        ):
            assert keep_alive.local_refresh_due() is False

    def test_the_windows_expiry_is_read_in_epoch_seconds(self):
        from scripts.sidecar_pkg import keep_alive

        expiry = _in_an_hour()
        with (
            patch.object(keep_alive.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_json_credential",
                return_value=_blob(expiry),
            ),
        ):
            epoch = keep_alive.local_expiry_epoch()
        assert epoch == pytest.approx(expiry.timestamp(), abs=1)

    def test_the_credential_store_is_read_once_per_tick(self):
        """The tick reads the store once: the presence check and the expiry
        must not each pay for their own read."""
        from scripts.sidecar_pkg import keep_alive

        blob = json.dumps(_blob(_in_an_hour())).encode()
        with (
            patch.object(keep_alive.platform, "system", return_value="Windows"),
            patch(
                "scripts.sidecar_pkg.wincred.read_credential_bytes",
                return_value=blob,
            ) as read,
        ):
            assert keep_alive.local_refresh_due() is False
        assert read.call_count == 1
