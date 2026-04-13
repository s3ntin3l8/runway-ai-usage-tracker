"""Unit tests for app/models/builder.py (LimitCardBuilder)."""
import pytest

from app.models.builder import LimitCardBuilder


class TestLimitCardBuilderInit:
    def test_raises_for_empty_service_name(self):
        with pytest.raises(ValueError, match="service_name"):
            LimitCardBuilder("", "🟠", "80%", "tokens")

    def test_raises_for_empty_icon(self):
        with pytest.raises(ValueError, match="icon"):
            LimitCardBuilder("Claude Pro", "", "80%", "tokens")

    def test_raises_for_none_remaining(self):
        with pytest.raises((ValueError, TypeError)):
            LimitCardBuilder("Claude Pro", "🟠", None, "tokens")

    def test_raises_for_none_unit(self):
        with pytest.raises((ValueError, TypeError)):
            LimitCardBuilder("Claude Pro", "🟠", "80%", None)


class TestLimitCardBuilderBuild:
    def test_build_returns_dict_with_mandatory_fields(self):
        card = LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens").build()
        assert isinstance(card, dict)
        for field in ("service_name", "icon", "remaining", "unit", "reset", "pace", "health"):
            assert field in card, f"Missing mandatory field: {field}"

    def test_build_default_values(self):
        card = LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens").build()
        assert card["reset"] == "—"
        assert card["pace"] == "Stable"
        assert card["health"] == "unknown"


class TestLimitCardBuilderSetProvider:
    def test_set_provider_sets_provider_id_and_window_type(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_provider("anthropic", window_type="rolling")
            .build()
        )
        assert card["provider_id"] == "anthropic"
        assert card["window_type"] == "rolling"

    def test_set_provider_default_window_type(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_provider("anthropic")
            .build()
        )
        assert card["provider_id"] == "anthropic"
        assert card["window_type"] == "unknown"


class TestLimitCardBuilderSetAccount:
    def test_set_account_with_both_values(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_account(account_id="acc123", account_label="user@example.com")
            .build()
        )
        assert card["account_id"] == "acc123"
        assert card["account_label"] == "user@example.com"

    def test_set_account_with_only_id(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_account(account_id="acc123")
            .build()
        )
        assert card["account_id"] == "acc123"
        assert card.get("account_label") is None

    def test_set_account_with_only_label(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_account(account_label="user@example.com")
            .build()
        )
        assert card.get("account_id") is None
        assert card["account_label"] == "user@example.com"


class TestLimitCardBuilderSetModel:
    def test_set_model_sets_model_id(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_model("claude-3-5-sonnet")
            .build()
        )
        assert card["model_id"] == "claude-3-5-sonnet"


class TestLimitCardBuilderSetSidecar:
    def test_set_sidecar_sets_sidecar_id(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_sidecar("host.example.com")
            .build()
        )
        assert card["sidecar_id"] == "host.example.com"


class TestLimitCardBuilderSetHealth:
    def test_set_health_overrides_default(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_health("good")
            .build()
        )
        assert card["health"] == "good"

    def test_set_health_critical(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_health("critical")
            .build()
        )
        assert card["health"] == "critical"


class TestLimitCardBuilderSetTiming:
    def test_set_timing_without_reset_at(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_timing(reset="in 2h 30m")
            .build()
        )
        assert card["reset"] == "in 2h 30m"
        assert card.get("reset_at") is None

    def test_set_timing_with_reset_at(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_timing(reset="in 2h 30m", reset_at="2026-04-13T12:00:00Z")
            .build()
        )
        assert card["reset"] == "in 2h 30m"
        assert card["reset_at"] == "2026-04-13T12:00:00Z"


class TestLimitCardBuilderSetUsage:
    def test_set_usage_sets_all_fields(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_usage(used_value=800.0, limit_value=1000.0, unit_type="tokens")
            .build()
        )
        assert card["used_value"] == 800.0
        assert card["limit_value"] == 1000.0
        assert card["unit_type"] == "tokens"
        assert card["is_unlimited"] is False
        assert card.get("currency") is None

    def test_set_usage_with_currency(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "$5.00", "USD")
            .set_usage(used_value=5.0, limit_value=10.0, unit_type="currency", currency="USD")
            .build()
        )
        assert card["currency"] == "USD"

    def test_set_usage_is_unlimited(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "∞", "tokens")
            .set_usage(used_value=0.0, limit_value=0.0, unit_type="tokens", is_unlimited=True)
            .build()
        )
        assert card["is_unlimited"] is True


class TestLimitCardBuilderSetPace:
    def test_set_pace_overrides_default(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_pace("Moderate Burn")
            .build()
        )
        assert card["pace"] == "Moderate Burn"


class TestLimitCardBuilderSetDetail:
    def test_set_detail_sets_detail(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_detail("20% used [OAuth]")
            .build()
        )
        assert card["detail"] == "20% used [OAuth]"


class TestLimitCardBuilderSetTier:
    def test_set_tier_sets_tier(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_tier("Pro")
            .build()
        )
        assert card["tier"] == "Pro"


class TestLimitCardBuilderSetUsageUrl:
    def test_set_usage_url_sets_usage_url(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_usage_url("https://console.anthropic.com/usage")
            .build()
        )
        assert card["usage_url"] == "https://console.anthropic.com/usage"


class TestLimitCardBuilderSetMetadata:
    def test_set_metadata_sets_metadata(self):
        meta = {"key1": "value1", "key2": 42}
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_metadata(meta)
            .build()
        )
        assert card["metadata"] == meta


class TestLimitCardBuilderSetErrorType:
    def test_set_error_type_sets_error_type(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "ERR", "tokens")
            .set_error_type("auth_failed")
            .build()
        )
        assert card["error_type"] == "auth_failed"


class TestLimitCardBuilderErrorClassMethod:
    def test_error_returns_dict_with_critical_health(self):
        card = LimitCardBuilder.error("Claude Pro", "🟠", "Auth failed", "auth_failed")
        assert isinstance(card, dict)
        assert card["health"] == "critical"

    def test_error_returns_remaining_err(self):
        card = LimitCardBuilder.error("Claude Pro", "🟠", "Auth failed", "auth_failed")
        assert card["remaining"] == "ERR"

    def test_error_sets_error_type(self):
        card = LimitCardBuilder.error("Claude Pro", "🟠", "Auth failed", "auth_failed")
        assert card["error_type"] == "auth_failed"

    def test_error_with_provider_id_sets_provider_id(self):
        card = LimitCardBuilder.error(
            "Claude Pro", "🟠", "Auth failed", "auth_failed", provider_id="anthropic"
        )
        assert card["provider_id"] == "anthropic"

    def test_error_without_provider_id_has_no_provider_id(self):
        card = LimitCardBuilder.error("Claude Pro", "🟠", "Auth failed", "auth_failed")
        # provider_id should be None (not set via set_provider)
        assert card.get("provider_id") is None

    def test_error_default_error_type(self):
        card = LimitCardBuilder.error("Claude Pro", "🟠", "Something went wrong")
        assert card["error_type"] == "unknown"


class TestLimitCardBuilderFluentChaining:
    def test_all_setters_return_self(self):
        builder = LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
        assert isinstance(builder.set_provider("anthropic"), LimitCardBuilder)
        assert isinstance(builder.set_account(account_id="a"), LimitCardBuilder)
        assert isinstance(builder.set_model("claude-3"), LimitCardBuilder)
        assert isinstance(builder.set_sidecar("host"), LimitCardBuilder)
        assert isinstance(builder.set_health("good"), LimitCardBuilder)
        assert isinstance(builder.set_timing("in 1h"), LimitCardBuilder)
        assert isinstance(builder.set_usage(1.0, 10.0, "tokens"), LimitCardBuilder)
        assert isinstance(builder.set_pace("Stable"), LimitCardBuilder)
        assert isinstance(builder.set_detail("some detail"), LimitCardBuilder)
        assert isinstance(builder.set_tier("Pro"), LimitCardBuilder)
        assert isinstance(builder.set_usage_url("https://example.com"), LimitCardBuilder)
        assert isinstance(builder.set_metadata({"k": "v"}), LimitCardBuilder)
        assert isinstance(builder.set_error_type("auth_failed"), LimitCardBuilder)

    def test_fluent_chain_builds_correctly(self):
        card = (
            LimitCardBuilder("Claude Pro", "🟠", "80%", "tokens")
            .set_provider("anthropic", window_type="rolling")
            .set_account(account_id="acc1", account_label="user@example.com")
            .set_health("good")
            .set_timing("in 2h", reset_at="2026-04-13T14:00:00Z")
            .set_usage(800.0, 1000.0, "tokens")
            .set_pace("Moderate Burn")
            .set_detail("80% remaining")
            .set_tier("Pro")
            .build()
        )
        assert card["service_name"] == "Claude Pro"
        assert card["provider_id"] == "anthropic"
        assert card["health"] == "good"
        assert card["pace"] == "Moderate Burn"
        assert card["tier"] == "Pro"
