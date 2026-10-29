"""List contract evidence contains categories only, never provider values."""

import json

import pytest


def observe(payload):
    from scripts.claude_cloud_list_boundary import observe_list_boundary

    return observe_list_boundary(payload, b"k" * 32, set(), set())


@pytest.mark.parametrize(
    "cursor,state,supported",
    [
        (None, "null", True),
        ("", "empty_string", False),
        ("opaque-private-cursor", "nonempty_string", True),
        (True, "invalid", False),
        ("x" * 2049, "invalid", False),
        ("bad\ncursor", "invalid", False),
    ],
    ids=["null", "empty", "opaque", "boolean", "oversized", "control"],
)
def test_cursor_evidence_distinguishes_current_contract_without_guessing_termination(
    cursor, state, supported
):
    result = observe(
        {"data": [{"id": "private-session"}], "next_cursor": cursor, "has_more": False}
    )
    assert result["cursor_state"] == state and result["cursor_supported"] is supported
    assert result["has_more_state"] == "false"
    assert result["record_count"] == result["valid_ids"] == 1
    assert "private-" not in json.dumps(result)
    assert "opaque-" not in json.dumps(result)


def test_missing_and_unavailable_fields_remain_distinct():
    result = observe({})
    assert result["top_level_kind"] == "object" and result["data_kind"] == "missing"
    assert result["cursor_state"] == result["has_more_state"] == "missing"
    assert result["record_count"] is None and result["checked_records"] == 0
    assert result["cursor_repeated"] is None
    assert observe([])["data_kind"] == "not_object"


def test_exact_previous_page_comparison_keeps_no_identifiers_or_digests():
    from scripts.claude_cloud_list_boundary import job_digest, observe_list_boundary

    key = b"k" * 32
    sid = "private-session"
    cursor = "private-cursor"
    seen = {job_digest(key, b"listed-session", sid)}
    cursors = {job_digest(key, b"cursor", ["listing", cursor])}
    result = observe_list_boundary(
        {
            "data": [{"id": sid}, {"id": sid}, {"id": "invalid/session"}, {}],
            "next_cursor": cursor,
            "has_more": True,
        },
        key,
        seen,
        cursors,
    )
    assert result["valid_ids"] == 2 and result["duplicate_valid_ids"] == 1
    assert result["previously_seen_ids"] == 2 and result["cursor_repeated"] is True
    assert result["invalid_ids"] == 2
    encoded = json.dumps(result)
    assert sid not in encoded and cursor not in encoded
    assert not any(value in encoded for value in seen | cursors)


def test_record_and_id_types_are_bounded_and_allowlisted():
    result = observe(
        {
            "data": [None, {"id": None}, {"id": True}, {"id": 3}, {"id": []}, {"id": {}}, {}] * 4,
            "next_cursor": None,
            "has_more": "private-value",
        }
    )
    assert result["record_count"] == 28 and result["checked_records"] == 20
    assert result["records_over_limit"] is True
    assert sum(result["id_kinds"].values()) == 20
    assert set(result["id_kinds"]) == {
        "missing",
        "null",
        "boolean",
        "number",
        "string",
        "array",
        "object",
        "not_object",
    }
    assert result["has_more_state"] == "invalid"
    assert "private-value" not in json.dumps(result)


def test_runtime_context_admits_the_new_shared_digest_and_classifier_module():
    from pathlib import Path

    assert "COPY scripts/claude_cloud_list_boundary.py ./scripts/" in Path("Dockerfile").read_text()
    assert "!scripts/claude_cloud_list_boundary.py" in Path(".dockerignore").read_text()


def test_digest_protocol_is_compatible_with_already_encrypted_checkpoints():
    from scripts.claude_cloud_list_boundary import job_digest

    assert (
        job_digest(b"k" * 32, b"listed-session", "fixture-session")
        == (
            "46dd1f74d50f18827a051c645a405df540bdb8581b2ee0615851e2b096077be1"  # pragma: allowlist secret - synthetic vector
        )
    )  # pragma: allowlist secret - synthetic compatibility vector


@pytest.mark.parametrize(
    "body,category,duplicates,nonfinite",
    [
        (b'{"data":[],"data":[],"next_cursor":null}', "duplicate_keys", 1, 0),
        (b'{"data":[],"extra":NaN,"next_cursor":null}', "nonfinite_values", 0, 1),
        (b"{broken", "invalid_json", 0, 0),
        (b"\xff", "encoding_error", 0, 0),
    ],
)
def test_strict_json_failures_are_categorized_without_publishing_body(
    body, category, duplicates, nonfinite
):
    from scripts.claude_cloud_list_boundary import BoundaryDecodeError, decode_boundary_json

    with pytest.raises(BoundaryDecodeError) as caught:
        decode_boundary_json(body)
    assert caught.value.category == category
    assert caught.value.duplicates == duplicates
    assert caught.value.nonfinite == nonfinite
    assert str(caught.value) == "unsupported_json"


def test_source_digest_keeps_the_existing_nested_serialization_protocol():
    from scripts.claude_cloud_list_boundary import job_digest

    source = ({"oauth_token": "fixture-access"}, None, ("sidecar", "file:fixture"))
    assert (
        job_digest(b"k" * 32, b"source", source)
        == (
            "77aac5f2dc0efec62466c387bc2935644c5608c63ff2bf2845bc10d536c7b336"  # pragma: allowlist secret - synthetic vector
        )
    )  # pragma: allowlist secret - synthetic compatibility vector


def test_diagnostic_and_page_reader_share_the_same_session_id_validator():
    from app.services import claude_cloud_probe as probe
    from scripts.claude_cloud_list_boundary import SESSION_ID

    assert probe.SESSION_ID is SESSION_ID
