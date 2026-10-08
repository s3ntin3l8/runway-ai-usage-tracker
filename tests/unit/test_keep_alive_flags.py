"""Per-login keep-alive state helpers (``app/services/refresh_policy.py``)."""

import json

import pytest

from app.services.refresh_policy import keep_alive_for, parse_provider_flags


class TestParseProviderFlags:
    def test_reads_json_text_and_dicts(self):
        assert parse_provider_flags('{"xai": true, "chatgpt": false}') == {
            "xai": True,
            "chatgpt": False,
        }
        assert parse_provider_flags({"xai": True}) == {"xai": True}

    @pytest.mark.parametrize("raw", [None, "", "{not json", "[]", '"xai"', 3, '{"xai": "yes"}'])
    def test_anything_malformed_is_empty(self, raw):
        assert parse_provider_flags(raw) == {}

    def test_unknown_providers_and_non_bool_values_are_dropped(self):
        raw = json.dumps({"xai": True, "gemini": True, "chatgpt": "on", "anthropic": 1})
        assert parse_provider_flags(raw) == {"xai": True}


class TestKeepAliveFor:
    def _for(self, **kw):
        base = {
            "reported": None,
            "desired": None,
            "reported_providers": None,
            "desired_providers": None,
        }
        return keep_alive_for("xai", **{**base, **kw})

    def test_follows_the_sidecar_level_values_without_per_login_data(self):
        assert self._for(reported=True, desired=False) == (True, False)
        assert self._for() == (None, None)

    def test_a_per_login_report_wins_over_the_sidecar_level_boolean(self):
        assert self._for(reported=False, reported_providers={"xai": True}) == (True, None)
        # Reported for other logins only: this one still follows the sidecar-level boolean.
        assert self._for(reported=False, reported_providers={"chatgpt": True}) == (False, None)

    def test_a_per_login_override_wins_over_the_sidecar_level_one(self):
        assert self._for(desired=True, desired_providers={"xai": False}) == (None, False)
        assert self._for(desired=True, desired_providers={"chatgpt": False}) == (None, True)
        assert self._for(desired=None, desired_providers={"xai": True}) == (None, True)
