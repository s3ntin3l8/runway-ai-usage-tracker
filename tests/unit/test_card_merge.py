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
