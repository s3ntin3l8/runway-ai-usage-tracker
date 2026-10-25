"""Persist usage-only evidence, with job-scoped identities and strict counters."""

import json

import pytest

from app.services.claude_cloud_ledger import usage_only_record
from scripts.audit_claude_usage import audit_messages


def normalize(record, key=bytes(range(32))):
    return usage_only_record(record, key)


def assistant(**changes):
    counters = {
        "input_tokens": 10,
        "output_tokens": 1,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 3,
    }
    counters.update(changes)
    return {"type": "assistant", "message": {"id": "synthetic-message", "usage": counters}}


def test_checkpoint_has_only_usage_and_job_scoped_context():
    record = assistant()
    record.update({"session_id": "private-session", "cwd": "private-directory"})
    record["message"].update({"content": [{"text": "private-content"}], "model": "private-model"})
    value = normalize(record)
    assert set(value) == {"type", "message", "session_scope"}
    scope = value.pop("session_scope")
    assert len(scope) == 64 and set(scope) <= set("0123456789abcdef")
    assert (
        value
        == {
            "type": "assistant",
            "message": {
                "id": "f92a63c3eaf8c1c0e2c967c9c20d894d6367220a129a149a7ac03913ff9bd0a5",  # pragma: allowlist secret - public synthetic HMAC vector
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 2,
                    "cache_creation_input_tokens": 3,
                },
            },
        }
    )
    assert normalize(record, b"x" * 32)["message"]["id"] != value["message"]["id"]
    assert "private-" not in json.dumps(value)
    assert "synthetic-message" not in json.dumps(value)


@pytest.mark.parametrize("bad", [None, True, -1, 1.5, "10", 2**63, float("inf")])
def test_malformed_usage_does_not_become_zero_or_survive_checkpoint(bad):
    value = normalize(assistant(input_tokens=bad))
    assert value["message"]["usage"] is False
    audit = audit_messages([value])
    assert audit["assistant"]["input_cache_observations"] is None
    assert "invalid_assistant_usage" in audit["diagnostics"]


def test_duplicate_and_conflict_semantics_survive_hmac_identity():
    first = normalize(assistant())
    other_output = normalize(assistant(output_tokens=7))
    matching = audit_messages([first, other_output])
    assert matching["assistant"]["unique_messages"] == 1
    assert matching["assistant"]["duplicate_messages"] == 1
    assert matching["assistant"]["input_cache_observations"]["input_tokens"] == 10
    assert "output_placeholder_divergence" in matching["diagnostics"]
    conflicting = audit_messages([first, normalize(assistant(input_tokens=99))])
    assert conflicting["assistant"]["input_cache_observations"] is None
    assert conflicting["assistant"]["conflicting_messages"] == 1


def test_subagent_scope_is_retained_without_parent_identifier():
    value = normalize({**assistant(), "parent_tool_use_id": "private-parent"})
    assert isinstance(value["parent_tool_use_id"], str) and len(value["parent_tool_use_id"]) == 64
    assert "private-parent" not in json.dumps(value)
    assert audit_messages([value])["assistant"]["subagent_records"] == 1


def test_result_model_names_and_errors_become_fixed_usage_only_fields():
    value = normalize(
        {
            "type": "result",
            "subtype": "error_private-detail",
            "usage": None,
            "modelUsage": {
                "private-model": {
                    "inputTokens": 3,
                    "outputTokens": 9,
                    "cacheReadInputTokens": 4,
                    "cacheCreationInputTokens": 5,
                }
            },
            "result": "private-result",
            "errors": ["private-error"],
        }
    )
    assert "private-" not in json.dumps(value)
    audit = audit_messages([value])
    assert audit["result"]["error_results"] == 1
    assert audit["result"]["last_usage_observation"] is None
    assert audit["result"]["last_model_usage_observation"] == {
        "input_tokens": 3,
        "output_tokens": 9,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 5,
    }


@pytest.mark.parametrize(
    "raw,diagnostic",
    [
        (None, "missing_result_model_usage"),
        ({}, "missing_result_model_usage"),
        ([], "invalid_result_model_usage"),
        ({"private-model": {"inputTokens": 1}}, "invalid_result_model_usage"),
    ],
)
def test_missing_and_invalid_model_usage_remain_distinct(raw, diagnostic):
    value = normalize({"type": "result", "modelUsage": raw})
    assert diagnostic in audit_messages([value])["diagnostics"]
    assert "private-model" not in json.dumps(value)


def test_model_aggregate_overflow_stays_unknown():
    model = {
        "inputTokens": 2**62,
        "outputTokens": 1,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
    }
    value = normalize({"type": "result", "modelUsage": {"one": model, "two": model}})
    assert value["modelUsage"] is False
    assert "invalid_result_model_usage" in audit_messages([value])["diagnostics"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "private-text",
        {"payload": assistant()},
        {"type": "user", "message": assistant()},
        {"type": ["assistant"]},
    ],
)
def test_opaque_or_unrecognized_objects_are_never_unwrapped(value):
    assert normalize(value) is None


def test_invalid_key_cannot_create_linkable_or_unkeyed_identities():
    with pytest.raises(ValueError, match="^invalid_ledger_key$"):
        normalize(assistant(), b"")


def test_invalid_unicode_identity_stays_unknown_without_an_encoding_exception():
    record = assistant()
    record["message"]["id"] = "\ud800"
    value = normalize(record)
    assert value["message"]["id"] is None
    assert "missing_assistant_identity" in audit_messages([value])["diagnostics"]


def test_same_message_comparison_never_sums_output_or_duplicates():
    from app.services.claude_cloud_ledger import compare_usage_records

    local = [normalize(assistant())]
    cloud = [normalize(assistant()), normalize(assistant(output_tokens=9))]
    result = compare_usage_records(local, cloud)
    assert result == {
        "local_messages": 1,
        "cloud_messages": 1,
        "matched_messages": 1,
        "missing_messages": 0,
        "additional_cloud_messages": 0,
        "mismatched_messages": 0,
        "local_conflicts": 0,
        "cloud_conflicts": 0,
        "local_unusable_records": 0,
        "cloud_unusable_records": 0,
        "all_expected_input_cache_match": True,
        "cloud_contract_verified": False,
        "ready_for_ingestion": False,
    }
    assert "output_tokens" not in json.dumps(result)
    assert "synthetic-message" not in json.dumps(result)


@pytest.mark.parametrize(
    "change,missing,mismatched,conflicts,unusable",
    [
        ("missing", 1, 0, 0, 0),
        ("different", 0, 1, 0, 0),
        ("conflict", 0, 0, 1, 0),
        ("invalid", 1, 0, 0, 1),
    ],
)
def test_partial_conflicting_or_invalid_comparisons_never_claim_match(
    change, missing, mismatched, conflicts, unusable
):
    from app.services.claude_cloud_ledger import compare_usage_records

    cloud = [] if change == "missing" else [normalize(assistant())]
    if change == "different":
        cloud = [normalize(assistant(input_tokens=99))]
    if change == "conflict":
        cloud.append(normalize(assistant(input_tokens=99)))
    if change == "invalid":
        cloud = [normalize(assistant(input_tokens="private-usage"))]
    result = compare_usage_records([normalize(assistant())], cloud)
    assert result["missing_messages"] == missing
    assert result["mismatched_messages"] == mismatched
    assert result["cloud_conflicts"] == conflicts
    assert result["cloud_unusable_records"] == unusable
    assert result["all_expected_input_cache_match"] is False
    assert "private-usage" not in json.dumps(result)


def test_empty_expected_records_and_different_job_salts_are_not_parity():
    from app.services.claude_cloud_ledger import compare_usage_records

    assert (
        compare_usage_records([], [normalize(assistant())])["all_expected_input_cache_match"]
        is False
    )
    result = compare_usage_records([normalize(assistant())], [normalize(assistant(), b"x" * 32)])
    assert result["matched_messages"] == 0
    assert result["missing_messages"] == result["additional_cloud_messages"] == 1
    assert result["all_expected_input_cache_match"] is False


def test_checkpoint_preserves_per_model_scopes_without_retaining_names():
    raw = {
        "type": "result",
        "modelUsage": {
            "private-model-one": {
                "inputTokens": 3,
                "outputTokens": 4,
                "cacheReadInputTokens": 5,
                "cacheCreationInputTokens": 6,
            },
            "private-model-two": {
                "inputTokens": 7,
                "outputTokens": 8,
                "cacheReadInputTokens": 9,
                "cacheCreationInputTokens": 10,
            },
        },
    }
    value = normalize(raw)
    assert len(value["modelUsage"]) == 2
    assert all(len(key) == 64 for key in value["modelUsage"])
    assert "private-" not in json.dumps(value)
    assert set(normalize(raw, b"x" * 32)["modelUsage"]).isdisjoint(value["modelUsage"])
    assert audit_messages([value])["result"]["last_model_usage_observation"] == {
        "input_tokens": 10,
        "output_tokens": 12,
        "cache_read_input_tokens": 14,
        "cache_creation_input_tokens": 16,
    }


def test_checkpoint_retains_domain_separated_session_request_and_parent_scopes():
    record = assistant()
    record["message"]["id"] = "private-shared"
    record["message"]["requestId"] = "private-shared"
    record.update(
        session_id="private-shared", parent_tool_use_id="private-shared", version="2.1.45"
    )
    value = normalize(record)
    hashes = {
        value["message"]["id"],
        value["session_scope"],
        value["request_scope"],
        value["parent_tool_use_id"],
    }
    assert len(hashes) == 4 and all(len(item) == 64 for item in hashes)
    second = normalize(record, b"x" * 32)
    assert hashes.isdisjoint(
        {
            second["message"]["id"],
            second["session_scope"],
            second["request_scope"],
            second["parent_tool_use_id"],
        }
    )
    assert value["record_version"] == "2.1.45"
    assert "private-" not in json.dumps(value)
    assert audit_messages([value])["assistant"]["subagent_records"] == 1


def test_local_and_sdk_session_keys_correlate_without_retaining_raw_identity():
    sdk = normalize({**assistant(), "session_id": "private-session"})
    local = normalize({**assistant(), "sessionId": "private-session"})
    assert sdk["session_scope"] == local["session_scope"]
    assert "private-session" not in json.dumps([sdk, local])


def test_conflicting_scope_metadata_cannot_claim_an_input_cache_match():
    from app.services.claude_cloud_ledger import compare_usage_records

    cloud = normalize({**assistant(), "session_id": "private-one", "sessionId": "private-two"})
    assert cloud["scope_conflict"] is True
    assert "session_scope" not in cloud
    result = compare_usage_records([normalize(assistant())], [cloud])
    assert result["cloud_unusable_records"] == 1
    assert result["all_expected_input_cache_match"] is False


def test_init_context_keeps_only_fixed_scope_and_version_metadata():
    value = normalize(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "private-session",
            "version": "2.1.45",
            "tools": [{"input": "private-input"}],
            "cwd": "private-directory",
            "apiKeySource": "private-key-source",  # pragma: allowlist secret - synthetic privacy fixture
        }
    )
    assert set(value) == {"type", "subtype", "session_scope", "record_version"}
    assert value["type"] == "system" and value["subtype"] == "init"
    assert len(value["session_scope"]) == 64 and value["record_version"] == "2.1.45"
    assert "private-" not in json.dumps(value)
    assert normalize({"type": "system", "subtype": "private-unknown"}) is None


def test_malformed_scope_metadata_is_unknown_without_retaining_strings_or_arrays():
    value = normalize(
        {**assistant(), "session_id": ["private-session"], "version": "private-version-text"}
    )
    assert value["scope_unknown"] is True
    assert "session_scope" not in value and "record_version" not in value
    assert "private-" not in json.dumps(value)


def test_subagent_comparison_requires_the_same_parent_scope_without_consumption_claim():
    from app.services.claude_cloud_ledger import compare_subagent_records

    left = normalize({**assistant(), "parent_tool_use_id": "private-parent"})
    same = normalize({**assistant(output_tokens=9), "parent_tool_use_id": "private-parent"})
    other = normalize({**assistant(), "parent_tool_use_id": "private-other-parent"})
    assert compare_subagent_records([left], [same])["all_expected_input_cache_match"] is True
    assert compare_subagent_records([left], [other])["missing_messages"] == 1
    assert compare_subagent_records([left], [other])["all_expected_input_cache_match"] is False
    assert compare_subagent_records([left], [same])["ready_for_ingestion"] is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1970-01-01T00:00:01.500Z", 1.5),
        ("1970-01-01T01:00:01.500+01:00", 1.5),
        ("1970-01-01T00:00:01.500", None),
        (1.5, None),
        (True, None),
        ("private-invalid-timestamp", None),
    ],
)
def test_usage_timestamps_require_explicit_timezone_and_never_guess_units(raw, expected):
    from app.services.claude_cloud_ledger import usage_timestamp

    assert usage_timestamp(raw) == expected


def test_sdk_timestamp_is_normalized_without_preserving_raw_text():
    value = normalize({**assistant(), "timestamp": "1970-01-01T01:00:01.500+01:00"})
    assert value["sdk_timestamp"] == 1.5
    assert "1970-" not in json.dumps(value)
