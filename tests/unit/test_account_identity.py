import hashlib

from app.services.account_identity import resolve_account_id


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
        expected = hashlib.sha256(hint.encode()).hexdigest()[:12]
        assert result == expected
        assert len(result) == 12

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
        expected = hashlib.sha256(hint.encode()).hexdigest()[:12]
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
        expected = hashlib.sha256(hint.encode()).hexdigest()[:12]
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
        expected = hashlib.sha256(hint.encode()).hexdigest()[:12]
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
