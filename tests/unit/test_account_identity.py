import hashlib

import pytest

from app.services.account_identity import (
    credential_fingerprint,
    keyed_credential_origin,
    normalize_sidecar_id,
    resolve_account_id,
    split_keyed_origin,
)

# Pinned fingerprints. Held as constants so detect-secrets' allowlist pragma
# lives in one place instead of trailing every assertion; what they pin
# (salt / iteration count / truncation length) is asserted in
# TestCredentialFingerprint.test_known_vectors.
PINNED_FP = "495fa9c614ce"  # pragma: allowlist secret
PINNED_FP_ALT = "84620f3735b3"  # pragma: allowlist secret


class TestResolveAccountId:
    def test_email_in_account_label_with_default_raw_id(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label="user@example.com",
        )
        assert result == "user@example.com"

    def test_email_in_account_label_with_none_raw_id(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id=None,
            account_label="user@example.com",
        )
        assert result == "user@example.com"

    def test_uuid_in_raw_account_id_without_label(self):
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id=uuid,
            account_label=None,
        )
        assert result == uuid

    def test_default_raw_id_with_credential_hint(self):
        hint = "sk-ant-v8-xxxxx"
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label=None,
            credential_hint=hint,
        )
        expected = hashlib.pbkdf2_hmac("sha256", hint.encode(), b"runway-account-id-v1", 1).hex()
        assert result == expected
        assert (
            len(result) == 64
        )  # PBKDF2-HMAC-SHA256 = 64 hex chars (SHA-256 output)  # full PBKDF2-HMAC-SHA256, no truncation

    def test_all_none_defaults(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label=None,
        )
        assert result == "default"

    def test_mixed_case_email_in_account_label(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label="User@Example.COM",
        )
        assert result == "user@example.com"

    def test_mixed_case_email_in_raw_account_id(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="User@Example.COM",
            account_label=None,
        )
        assert result == "user@example.com"

    def test_empty_string_raw_id_falls_to_default(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="",
            account_label=None,
        )
        assert result == "default"

    def test_empty_string_raw_id_with_hint(self):
        hint = "sk-ant-v8-xxxxx"
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="",
            account_label=None,
            credential_hint=hint,
        )
        expected = hashlib.pbkdf2_hmac("sha256", hint.encode(), b"runway-account-id-v1", 1).hex()
        assert result == expected

    def test_non_email_account_label_falls_through(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label="Runway User",
        )
        assert result == "default"

    def test_non_email_account_label_with_hint(self):
        hint = "sk-ant-v8-xxxxx"
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label="Runway User",
            credential_hint=hint,
        )
        expected = hashlib.pbkdf2_hmac("sha256", hint.encode(), b"runway-account-id-v1", 1).hex()
        assert result == expected

    def test_custom_raw_id_returned_as_is(self):
        result = resolve_account_id(
            provider_id="opencode",
            raw_account_id="opencode-go",
            account_label=None,
        )
        assert result == "opencode-go"

    def test_account_label_priority_over_raw_id(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="550e8400-e29b-41d4-a716-446655440000",
            account_label="user@example.com",
        )
        assert result == "user@example.com"

    def test_email_detection_requires_dot(self):
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="user@localhost",
            account_label=None,
        )
        assert result == "user@localhost"

    def test_none_raw_id_with_none_label_and_hint(self):
        hint = "sk-ant-v8-xxxxx"
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id=None,
            account_label=None,
            credential_hint=hint,
        )
        expected = hashlib.pbkdf2_hmac("sha256", hint.encode(), b"runway-account-id-v1", 1).hex()
        assert result == expected

    def test_email_with_trailing_garbage_not_matched(self):
        """Test that regex anchor prevents matching emails with trailing garbage."""
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label="user@example.com-extra",
        )
        assert result == "default"

    def test_email_with_long_tld(self):
        """Test that long TLDs like .photography are accepted."""
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label="user@example.photography",
        )
        assert result == "user@example.photography"

    def test_email_at_org_format_extracts_email(self):
        """Anthropic sets account_label = 'email @ org' — extract the email part."""
        result = resolve_account_id(
            provider_id="anthropic",
            raw_account_id="default",
            account_label="user@company.com @ MyOrg",
        )
        assert result == "user@company.com"


class TestNormalizeSidecarId:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # The macbook flap collapses to one stable id.
            ("macbook.local", "macbook"),
            ("Macbook.in.s3ntin3l8.de", "macbook"),
            ("macbook", "macbook"),
            ("MacBook", "macbook"),
            # Bare lowercase short names are untouched.
            ("mgmt", "mgmt"),
            ("dockerhost", "dockerhost"),
            ("dev-01", "dev-01"),
            # A short name that later gains a domain still collapses back.
            ("dev-01.lan", "dev-01"),
            # Sentinels and literals pass through (lowercased).
            ("local", "local"),
            ("192.168.1.5", "192.168.1.5"),
            ("", ""),
            ("  Host.Example.COM  ", "host"),
        ],
    )
    def test_normalization(self, raw, expected):
        assert normalize_sidecar_id(raw) == expected

    def test_idempotent(self):
        for raw in ("macbook.local", "Macbook.in.s3ntin3l8.de", "dev-01", "local"):
            once = normalize_sidecar_id(raw)
            assert normalize_sidecar_id(once) == once


class TestCredentialFingerprint:
    """Key-scoped credential origins (#347).

    The sidecar and the server independently derive both halves of a
    fingerprint-keyed origin — the sidecar from the credential it found on
    disk, the server from the credential the operator pasted into
    ``provider_configs`` — so any drift between the two implementations
    silently drops the association the whole feature exists to make.
    """

    def test_known_vectors(self):
        # Pinned so a change to the salt, iteration count, or truncation
        # length fails here instead of quietly orphaning every stored tag.
        assert credential_fingerprint("oc_sk_test_key") == PINNED_FP
        assert credential_fingerprint("sk-or-v1-abc") == PINNED_FP_ALT

    def test_is_twelve_lower_hex(self):
        fp = credential_fingerprint("oc_sk_test_key")
        assert fp is not None
        assert len(fp) == 12
        assert all(c in "0123456789abcdef" for c in fp)

    @pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
    def test_blank_input_returns_none(self, blank):
        assert credential_fingerprint(blank) is None

    def test_whitespace_is_stripped(self):
        assert credential_fingerprint("  oc_spaced  ") == credential_fingerprint("oc_spaced")

    def test_distinct_credentials_distinct_fingerprints(self):
        keys = ["oc_sk_alpha", "oc_sk_beta", "oc_sk_gamma"]
        fps = {credential_fingerprint(k) for k in keys}
        assert len(fps) == len(keys)

    def test_fingerprint_is_stable_across_calls(self):
        assert credential_fingerprint("oc_sk_stable") == credential_fingerprint("oc_sk_stable")

    def test_server_and_sidecar_implementations_agree(self):
        from scripts.sidecar_pkg import identity as sidecar_identity

        samples = ["oc_sk_test_key", "sk-or-v1-abc", "oc_sk_beta", None, ""]
        for s in samples:
            assert credential_fingerprint(s) == sidecar_identity.credential_fingerprint(s)
        for s in ("oc_sk_test_key", "sk-or-v1-abc"):
            fp = credential_fingerprint(s)
            assert fp is not None
            assert keyed_credential_origin(
                "path:/tmp/auth.json", fp
            ) == sidecar_identity.keyed_credential_origin("path:/tmp/auth.json", fp)
        for origin in (
            "path:/tmp/auth.json#495fa9c614ce",
            "env:OPENCODE_API_KEY#495fa9c614ce",
            "path:/tmp/auth.json",
            "provider:opencode",
            "path:/tmp/auth.json#not-a-fingerprint",
            "path:/tmp/auth.json#495fa9c614CE",  # uppercase is not our format
        ):
            assert split_keyed_origin(origin) == sidecar_identity.split_keyed_origin(origin)


class TestKeyedCredentialOrigin:
    def test_appends_fingerprint(self):
        base = "path:/home/u/.local/share/opencode/auth.json"
        assert keyed_credential_origin(base, PINNED_FP) == f"{base}#{PINNED_FP}"

    def test_provider_base_is_path_independent(self):
        # The server can build this key without knowing the sidecar's
        # home directory — that asymmetry is what makes T1 possible.
        fp = credential_fingerprint("oc_sk_test_key")
        assert fp is not None
        assert keyed_credential_origin("provider:opencode", fp) == f"provider:opencode#{fp}"

    @pytest.mark.parametrize(
        "origin,expected_base,expected_fp",
        [
            (f"path:/tmp/auth.json#{PINNED_FP}", "path:/tmp/auth.json", PINNED_FP),
            (f"env:OPENCODE_API_KEY#{PINNED_FP}", "env:OPENCODE_API_KEY", PINNED_FP),
            # No suffix — untouched, no fingerprint.
            ("path:/tmp/auth.json", "path:/tmp/auth.json", None),
            ("provider:opencode", "provider:opencode", None),
            # Suffix isn't 12 lower-hex — not ours, don't strip it.
            ("path:/tmp/auth.json#abc", "path:/tmp/auth.json#abc", None),
            (
                f"path:/tmp/auth.json#{PINNED_FP.upper()}",
                f"path:/tmp/auth.json#{PINNED_FP.upper()}",
                None,
            ),
            # A '#' inside the path survives: rpartition takes the LAST one.
            (
                f"path:/tmp/we#ird/auth.json#{PINNED_FP}",
                "path:/tmp/we#ird/auth.json",
                PINNED_FP,
            ),
        ],
    )
    def test_split_round_trip_and_edge_cases(self, origin, expected_base, expected_fp):
        base, fp = split_keyed_origin(origin)
        assert (base, fp) == (expected_base, expected_fp)
        if fp is not None:
            assert keyed_credential_origin(base, fp) == origin

    def test_round_trip_for_every_generated_fingerprint(self):
        for key in ("oc_sk_a", "oc_sk_b", "sk-or-v1-c"):
            fp = credential_fingerprint(key)
            assert fp is not None
            origin = keyed_credential_origin("path:/x/auth.json", fp)
            assert split_keyed_origin(origin) == ("path:/x/auth.json", fp)
