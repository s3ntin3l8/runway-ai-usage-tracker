"""Offline usage evidence must not invent consumption or leak session content."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.audit_claude_usage import AuditInputError, audit_messages, read_records


def assistant(identifier="message-a", **updates):
    usage = {
        "input_tokens": 10,
        "output_tokens": 1,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 3,
    }
    usage.update(updates)
    return {"type": "assistant", "message": {"id": identifier, "usage": usage}}


def test_duplicate_assistant_blocks_count_input_once_and_never_claim_output_total():
    message = assistant()
    report = audit_messages([message, message, assistant("message-b", input_tokens=7)])
    assert report["assistant"]["unique_messages"] == 2
    assert report["assistant"]["duplicate_messages"] == 1
    assert report["assistant"]["input_cache_observations"] == {
        "input_tokens": 17,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 6,
    }
    assert report["ready_for_ingestion"] is False
    assert report["cloud_contract_verified"] is False
    assert "output_tokens" not in report["assistant"]["input_cache_observations"]


def test_changed_usage_for_same_message_invalidates_aggregate_in_both_orders():
    first = assistant()
    correction = assistant(output_tokens=90)
    for records in ([first, correction], [correction, first]):
        report = audit_messages(records)
        assert report["assistant"]["conflicting_messages"] == 1
        assert report["assistant"]["input_cache_observations"] is None
        assert "conflicting_assistant_usage" in report["diagnostics"]


@pytest.mark.parametrize("value", [True, -1, 1.5, "12", None, 2**63])
def test_invalid_counters_cannot_become_numeric_observations(value):
    report = audit_messages([assistant(input_tokens=value)])
    assert report["assistant"]["input_cache_observations"] is None
    assert "invalid_assistant_usage" in report["diagnostics"]


def test_missing_counter_and_missing_id_are_unknown_not_zero():
    message = assistant()
    del message["message"]["usage"]["cache_read_input_tokens"]
    report = audit_messages([message, {"type": "assistant", "message": {"usage": {}}}])
    assert report["assistant"]["input_cache_observations"] is None
    assert set(report["diagnostics"]) == {
        "missing_assistant_identity",
        "invalid_assistant_usage",
        "no_usage_observations",
    }


def test_result_scopes_are_separate_and_snapshots_are_never_summed():
    results = []
    for main_output, tree_output in [(30, 100), (50, 150), (2, 5)]:
        results.append(
            {
                "type": "result",
                "usage": assistant(output_tokens=main_output)["message"]["usage"],
                "modelUsage": {
                    "private-model": {
                        "inputTokens": 80,
                        "outputTokens": tree_output,
                        "cacheReadInputTokens": 4,
                        "cacheCreationInputTokens": 2,
                    }
                },
            }
        )
    report = audit_messages(results)
    assert report["result"]["usage_observations"] == 3
    assert report["result"]["last_usage_observation"]["output_tokens"] == 2
    assert report["result"]["last_model_usage_observation"]["output_tokens"] == 5
    assert report["result"]["usage_decreases"] == 1
    assert report["result"]["model_usage_decreases"] == 1
    assert "result_scope_unverified" in report["diagnostics"]


def test_missing_error_result_does_not_reuse_previous_snapshot_as_final_usage():
    report = audit_messages(
        [
            {"type": "result", "usage": assistant()["message"]["usage"]},
            {"type": "result", "subtype": "error_during_execution", "usage": None},
        ]
    )
    assert report["result"]["last_usage_observation"] is None
    assert report["result"]["usage_observations"] == 1
    assert "missing_result_usage" in report["diagnostics"]


def test_subagent_records_do_not_get_added_to_main_agent_input():
    subagent = assistant("child")
    subagent["parent_tool_use_id"] = "private-tool"
    report = audit_messages([assistant(), subagent])
    assert report["assistant"]["subagent_records"] == 1
    assert report["assistant"]["input_cache_observations"]["input_tokens"] == 10


def test_empty_and_unsupported_cloud_envelope_remain_incomplete():
    assert "no_usage_observations" in audit_messages([])["diagnostics"]
    report = audit_messages([{"data": [{"type": "assistant"}]}])
    assert "unsupported_record" in report["diagnostics"]
    assert report["status"] == "incomplete"


def test_no_identifier_content_or_untrusted_field_is_emitted():
    secret = "private-session-token-and-content"  # pragma: allowlist secret - synthetic sentinel
    message = assistant(secret)
    message["message"]["content"] = [{"type": "text", "text": secret}]
    message["session_id"] = secret
    report = audit_messages([message, {"type": secret, secret: secret}])
    assert secret not in json.dumps(report)
    assert report["diagnostics"].count("unsupported_record") == 1


@pytest.mark.parametrize(
    "raw,code",
    [
        (b'{"type":"private"\n', "invalid_json"),
        (b'{"type":"user","type":"assistant"}\n', "duplicate_json_key"),
        (b"NaN\n", "invalid_json"),
        (b"\xff\n", "invalid_encoding"),
    ],
)
def test_reader_errors_are_fixed_categories(raw, code):
    with pytest.raises(AuditInputError, match=f"^{code}$"):
        list(read_records(io.BytesIO(raw)))


def test_reader_limits_bytes_line_length_and_record_count():
    for kwargs in ({"max_line_bytes": 5}, {"max_bytes": 5}, {"max_records": 1}):
        with pytest.raises(AuditInputError, match="^input_limit_exceeded$"):
            list(read_records(io.BytesIO(b'{"type":"user"}\n' * 2), **kwargs))


def test_cli_failure_reports_no_raw_input_or_exception():
    script = Path(__file__).resolve().parents[2] / "scripts/audit_claude_usage.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        input=b'{"private-secret"\n',
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["diagnostics"] == ["invalid_json"]
    assert result.stderr == b""
    assert b"private-secret" not in result.stdout


def test_zero_error_snapshot_is_flagged_without_claiming_zero_consumption():
    report = audit_messages(
        [
            {
                "type": "result",
                "subtype": "error_during_execution",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        ]
    )
    assert report["result"]["error_results"] == 1
    assert "error_result_observed" in report["diagnostics"]
    assert report["ready_for_ingestion"] is False


def test_partial_model_map_is_not_reported_as_complete_tree_usage():
    report = audit_messages(
        [
            {
                "type": "result",
                "modelUsage": {
                    "one-model": {
                        "inputTokens": 10,
                        "outputTokens": 2,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                    },
                    "other-model": {"inputTokens": 20},
                },
            }
        ]
    )
    assert report["result"]["last_model_usage_observation"] is None
    assert "invalid_result_model_usage" in report["diagnostics"]


def test_diagnostics_are_deduplicated_and_cli_success_remains_observations_only():
    report = audit_messages([{"type": "unknown"}] * 3)
    assert report["diagnostics"].count("unsupported_record") == 1
    script = Path(__file__).resolve().parents[2] / "scripts/audit_claude_usage.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        input=(json.dumps(assistant()) + "\n").encode(),
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout)["ready_for_ingestion"] is False


def test_malformed_result_is_distinguished_from_an_absent_observation():
    report = audit_messages([{"type": "result", "usage": {"input_tokens": True}, "modelUsage": []}])
    assert "invalid_result_usage" in report["diagnostics"]
    assert "invalid_result_model_usage" in report["diagnostics"]
    assert report["result"]["last_usage_observation"] is None
