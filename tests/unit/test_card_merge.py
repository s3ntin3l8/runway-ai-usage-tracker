import json

from app.services.accumulator import merge_card_json


class TestCardMerge:
    def test_no_existing_card(self):
        result = merge_card_json(None, {"pct_used": 12.0, "service_name": "Test"})
        parsed = json.loads(result)
        assert parsed["pct_used"] == 12.0
        assert parsed["service_name"] == "Test"

    def test_existing_no_op_incoming_wins_for_common_fields(self):
        existing = json.dumps(
            {
                "pct_used": 12.0,
                "limit_value": 100.0,
                "token_usage": None,
            }
        )
        incoming = {
            "pct_used": None,
            "token_usage": {"total": 654000000},
        }
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["pct_used"] == 12.0
        assert parsed["limit_value"] == 100.0
        assert parsed["token_usage"]["total"] == 654000000

    def test_server_overwrites_pct_used(self):
        existing = json.dumps({"pct_used": 10.0})
        incoming = {"pct_used": 20.0}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["pct_used"] == 20.0

    def test_by_model_preserved_when_incoming_empty(self):
        existing = json.dumps({"by_model": {"sonnet": {"tokens": 100}}})
        incoming = {"by_model": {}}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["by_model"] == {"sonnet": {"tokens": 100}}

    def test_by_model_preserved_when_incoming_none(self):
        existing = json.dumps({"by_model": {"sonnet": {"tokens": 100}}})
        incoming = {"by_model": None}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["by_model"] == {"sonnet": {"tokens": 100}}

    def test_by_model_preserved_when_incoming_absent(self):
        existing = json.dumps({"by_model": {"sonnet": {"tokens": 100}}})
        incoming = {}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["by_model"] == {"sonnet": {"tokens": 100}}

    def test_by_model_replaced_when_incoming_non_empty(self):
        existing = json.dumps({"by_model": {"sonnet": {"tokens": 100}}})
        incoming = {"by_model": {"opus": {"tokens": 200}}}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["by_model"] == {"opus": {"tokens": 200}}

    def test_data_source_merge(self):
        existing = json.dumps({"data_source": "web"})
        incoming = {"data_source": "local"}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["data_source"] == "web,local"

    def test_data_source_no_dup(self):
        existing = json.dumps({"data_source": "web"})
        incoming = {"data_source": "web"}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["data_source"] == "web"

    def test_input_source_merge(self):
        existing = json.dumps({"input_source": "server"})
        incoming = {"input_source": "unknown"}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["input_source"] == "server,unknown"

    def test_full_fusion(self):
        existing = json.dumps(
            {
                "pct_used": 12.0,
                "limit_value": 100.0,
                "data_source": "web",
                "input_source": "server",
                "token_usage": None,
                "by_model": {},
            }
        )
        incoming = {
            "pct_used": None,
            "token_usage": {"total": 654000000},
            "by_model": {"sonnet": {"tokens": 100}},
            "data_source": "local",
            "input_source": "unknown",
        }
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["pct_used"] == 12.0
        assert parsed["limit_value"] == 100.0
        assert parsed["token_usage"]["total"] == 654000000
        assert parsed["by_model"] == {"sonnet": {"tokens": 100}}
        assert parsed["data_source"] == "web,local"
        assert parsed["input_source"] == "server,unknown"

    def test_empty_existing_string(self):
        result = merge_card_json("", {"pct_used": 5.0})
        parsed = json.loads(result)
        assert parsed["pct_used"] == 5.0

    def test_data_source_none_incoming(self):
        existing = json.dumps({"data_source": "web"})
        incoming = {"data_source": None}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["data_source"] == "web"

    def test_input_source_none_incoming(self):
        existing = json.dumps({"input_source": "server"})
        incoming = {"input_source": None}
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["input_source"] == "server"

    def test_data_source_no_accumulation_on_repeated_merge(self):
        # Three alternating poll cycles: server → sidecar → server again
        state = merge_card_json(None, {"data_source": "web"})
        state = merge_card_json(state, {"data_source": "local"})
        state = merge_card_json(state, {"data_source": "web"})
        assert json.loads(state)["data_source"] == "web,local"

    def test_fresh_quota_clears_stale_error_type(self):
        # Regression: a prior failed poll stamped error_type="auth_failed" onto a
        # row that later carried fresh quota. Because collectors send dicts with
        # exclude_none=True, the recovery card has no error_type key and the stale
        # stamp would otherwise stick forever, hiding the row from the dashboard
        # "Most Constrained" hero filter (which drops anything with error_type set).
        existing = json.dumps(
            {
                "used_value": 50.0,
                "limit_value": 100.0,
                "error_type": "auth_failed",
                "data_source": "error",
                "remaining": "ERR",
                "health": "good",
            }
        )
        incoming = {
            "used_value": 51.0,
            "limit_value": 100.0,
            "data_source": "api",
            "health": "good",
        }
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert "error_type" not in parsed
        assert parsed["data_source"] == "api"
        assert "remaining" not in parsed
        assert parsed["used_value"] == 51.0

    def test_token_only_enrichment_preserves_error_type(self):
        # Negative case: a local token-breakdown push without quota fields must
        # NOT mask a genuinely-still-erroring quota row. Only fresh quota signal
        # (used_value/limit_value/pct_used) is allowed to clear the error stamps.
        existing = json.dumps(
            {
                "error_type": "auth_failed",
                "data_source": "error",
                "remaining": "ERR",
            }
        )
        incoming = {
            "token_usage": {"total": 12345},
            "by_model": {"sonnet": {"tokens": 12345}},
            "data_source": "local",
        }
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["error_type"] == "auth_failed"
        # data_source still merges via _join_distinct (drops "error"), but error_type stays
        assert parsed["remaining"] == "ERR"

    def test_error_card_does_not_clear_existing_error(self):
        # A second error card landing on top of an existing error row leaves the
        # stamps intact — recovery clearing must only fire for non-error incoming.
        existing = json.dumps(
            {
                "error_type": "auth_failed",
                "data_source": "error",
            }
        )
        incoming = {
            "error_type": "rate_limited",
            "data_source": "error",
            "remaining": "ERR",
        }
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        assert parsed["error_type"] == "rate_limited"

    def test_unit_mismatch_protects_quota_fields(self):
        # A local token enrichment must not overwrite percent-based quota data.
        existing = json.dumps(
            {
                "unit_type": "percent",
                "used_value": 14.0,
                "limit_value": 100.0,
                "pct_used": None,
                "data_source": "web",
                "token_usage": None,
            }
        )
        incoming = {
            "unit_type": "tokens",
            "used_value": 30473683.0,
            "limit_value": None,
            "data_source": "local",
            "token_usage": {"total": 30473683},
        }
        result = merge_card_json(existing, incoming)
        parsed = json.loads(result)
        # Quota fields must be preserved from the percent-unit source
        assert parsed["unit_type"] == "percent"
        assert parsed["used_value"] == 14.0
        assert parsed["limit_value"] == 100.0
        # Non-quota enrichment fields should be merged normally
        assert parsed["token_usage"]["total"] == 30473683
        assert parsed["data_source"] == "web,local"
