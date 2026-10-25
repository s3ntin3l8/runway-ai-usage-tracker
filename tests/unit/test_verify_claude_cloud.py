"""Offline evidence can establish a sample, never live acceptance or ingestion."""

import copy
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.audit_claude_usage import audit_messages
from scripts.verify_claude_cloud import MAX_BYTES, EvidenceError, evaluate, read_evidence


def evidence():
    audit = audit_messages(
        [
            {
                "type": "assistant",
                "message": {
                    "id": "synthetic",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
        ]
    )
    return {
        "format_version": 1,
        "source_commit": "a" * 40,
        "image": "ghcr.io/s3ntin3l8/runway@sha256:" + "b" * 64,
        "observed_at": "2026-10-24T12:00:00Z",
        "deployment": "selected-production",
        "report": {
            "format_version": 1,
            "outcome": "sample_complete",
            "stage": "complete",
            "requests": 3,
            "http_status": 200,
            "account_verified": True,
            "organization_verified": True,
            "session_list_records": 1,
            "session_list_complete": True,
            "history_pages": 1,
            "history_events": 1,
            "history_complete": True,
            "sequence": {"numeric": 1, "invalid": 0, "duplicates": 0, "gaps": 0, "out_of_order": 0},
            "observations": [{"location": "data", "report": audit}],
            "cloud_contract_verified": False,
            "ready_for_ingestion": False,
        },
    }


def test_complete_sample_keeps_metadata_but_never_claims_acceptance():
    value = evidence()
    before = copy.deepcopy(value)
    result = evaluate(value)
    assert result["status"] == "sample_observed" and result["reasons"] == []
    assert result["source_commit"] == value["source_commit"]
    assert result["image"] == value["image"] and result["report"] == value["report"]
    for key in (
        "provenance_verified",
        "live_acceptance_passed",
        "cloud_contract_verified",
        "ready_for_ingestion",
    ):
        assert result[key] is False
    assert value == before


@pytest.mark.parametrize(
    "outcome",
    [
        "sample_limited",
        "sample_not_found",
        "no_sessions",
        "busy",
        "source_unavailable",
        "source_ineligible",
        "unsupported_source",
        "source_changed",
        "expired",
        "identity_mismatch",
        "identity_unavailable",
        "rejected",
        "rate_limited",
        "unavailable",
        "timeout",
        "response_limit",
        "unsupported_contract",
    ],
)
def test_every_noncomplete_outcome_stops_without_retry(outcome):
    value = evidence()
    value["report"]["outcome"] = outcome
    result = evaluate(value)
    assert result["status"] == "stop" and "probe_not_complete" in result["reasons"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("stage", "history"),
        ("account_verified", False),
        ("organization_verified", False),
        ("http_status", 403),
        ("history_complete", False),
        ("requests", 2),
        ("history_pages", 0),
        ("history_events", 0),
        ("session_list_records", 0),
    ],
)
def test_inconsistent_complete_sample_stops(field, value):
    item = evidence()
    item["report"][field] = value
    if field == "history_events":
        item["report"]["observations"] = []
    assert evaluate(item)["status"] == "stop"


@pytest.mark.parametrize("counter", ["invalid", "duplicates", "gaps", "out_of_order"])
def test_sequence_anomalies_require_manual_investigation(counter):
    value = evidence()
    value["report"]["sequence"][counter] = 1
    assert "sequence_requires_review" in evaluate(value)["reasons"]


def test_missing_or_multiple_usage_locations_stop_without_combining():
    value = evidence()
    value["report"]["observations"] = []
    assert "usage_requires_review" in evaluate(value)["reasons"]
    value = evidence()
    other = copy.deepcopy(value["report"]["observations"][0])
    other["location"] = "event"
    value["report"]["observations"].append(other)
    assert "usage_requires_review" in evaluate(value)["reasons"]


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("report",),
        ("report", "sequence"),
        ("report", "observations", 0),
        ("report", "observations", 0, "report"),
        ("report", "observations", 0, "report", "assistant"),
        ("report", "observations", 0, "report", "result"),
        ("report", "observations", 0, "report", "assistant", "input_cache_observations"),
    ],
)
def test_unknown_fields_at_every_depth_are_rejected(path):
    value = evidence()
    target = value
    for key in path:
        target = target[key]
    target["private-token"] = "private-transcript"
    with pytest.raises(EvidenceError, match="^invalid_evidence$"):
        evaluate(value)


@pytest.mark.parametrize("value", [True, -1, 1.5, "1", 2**63])
def test_counters_are_strict_nonnegative_bounded_integers(value):
    item = evidence()
    item["report"]["history_events"] = value
    with pytest.raises(EvidenceError):
        evaluate(item)


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "ghcr.io/s3ntin3l8/runway:edge"),
        ("image", "private-token"),
        ("source_commit", "main"),
        ("source_commit", "b" * 39),
        ("observed_at", "2026-10-24T12:00:00"),
        ("observed_at", "invalid"),
        ("deployment", "private-account"),
        ("format_version", True),
    ],
)
def test_mutable_builds_and_uncontrolled_metadata_are_rejected(field, value):
    item = evidence()
    item[field] = value
    with pytest.raises(EvidenceError):
        evaluate(item)


@pytest.mark.parametrize(
    "path",
    [
        ("cloud_contract_verified",),
        ("ready_for_ingestion",),
        ("observations", 0, "report", "ready_for_ingestion"),
    ],
)
def test_unverified_contract_flags_cannot_be_promoted(path):
    item = evidence()
    target = item["report"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = True
    with pytest.raises(EvidenceError):
        evaluate(item)


@pytest.mark.parametrize(
    "raw",
    [b'{"format_version":1,"format_version":1}', b"NaN", b"\xff", b"{}\n{}", b"[" * 1100],
    ids=["duplicate", "constant", "encoding", "multiple", "depth"],
)
def test_malformed_input_is_sanitized(raw):
    with pytest.raises(EvidenceError, match="^invalid_evidence$"):
        evaluate(read_evidence(io.BytesIO(raw)))


def test_reading_is_bounded_before_parsing():
    with pytest.raises(EvidenceError, match="^input_limit_exceeded$"):
        read_evidence(io.BytesIO(b"x" * (MAX_BYTES + 1)))


def test_cli_only_reads_stdin_and_emits_fixed_errors():
    root = Path(__file__).resolve().parents[2]
    for raw, code in ((json.dumps(evidence()).encode(), 0), (b"private-token", 2)):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.verify_claude_cloud"],
            input=raw,
            capture_output=True,
            cwd=root,
            timeout=10,
        )
        assert result.returncode == code
        assert b"private-token" not in result.stdout + result.stderr
        output = json.loads(result.stdout)
        assert output["live_acceptance_passed"] is False


@pytest.mark.parametrize("field", ["usage_decreases", "model_usage_decreases", "error_results"])
def test_resets_and_errors_need_review_even_if_status_claims_complete(field):
    item = evidence()
    item["report"]["history_events"] = 3
    item["report"]["sequence"]["numeric"] = 3
    result = item["report"]["observations"][0]["report"]["result"]
    result["records"] = 2
    for prefix in ("usage", "model_usage"):
        result[prefix + "_observations"] = 2
        result["last_" + prefix + "_observation"] = {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    result[field] = 1
    assert "usage_requires_review" in evaluate(item)["reasons"]


def test_impossible_observation_counts_and_unknown_diagnostics_are_rejected():
    item = evidence()
    item["report"]["observations"][0]["report"]["assistant"]["records"] = 151
    with pytest.raises(EvidenceError):
        evaluate(item)
    item = evidence()
    item["report"]["observations"][0]["report"]["diagnostics"] = ["private-token"]
    with pytest.raises(EvidenceError):
        evaluate(item)


@pytest.mark.parametrize("version", [1, 2])
def test_cli_runs_without_site_packages_or_application_imports(version):
    script = Path(__file__).resolve().parents[2] / "scripts/verify_claude_cloud.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        input=json.dumps(evidence() if version == 1 else structured_evidence()).encode(),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "sample_observed"


def test_a_limited_session_list_does_not_invalidate_the_selected_complete_sample():
    item = evidence()
    item["report"]["session_list_complete"] = False
    assert evaluate(item)["status"] == "sample_observed"


@pytest.mark.parametrize("which", ["usage", "model_usage"])
@pytest.mark.parametrize("contradiction", ["zero_count", "missing_last", "excess_decreases"])
def test_result_counter_payload_contradictions_are_rejected(which, contradiction):
    item = evidence()
    item["report"]["history_events"] = 2
    item["report"]["sequence"]["numeric"] = 2
    result = item["report"]["observations"][0]["report"]["result"]
    result["records"] = 1
    result[which + "_observations"] = 1
    result["last_" + which + "_observation"] = {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    if contradiction == "zero_count":
        result[which + "_observations"] = 0
    elif contradiction == "missing_last":
        result["last_" + which + "_observation"] = None
    else:
        result[which + "_decreases"] = 1
    with pytest.raises(EvidenceError):
        evaluate(item)


def test_cli_distinguishes_oversize_input_without_echoing_it():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "scripts.verify_claude_cloud"],
        input=b"x" * (MAX_BYTES + 1),
        capture_output=True,
        cwd=root,
        timeout=10,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["reasons"] == ["input_limit_exceeded"]
    assert result.stderr == b""


def test_cli_closed_output_pipe_exits_without_a_traceback():
    script = Path(__file__).resolve().parents[2] / "scripts/verify_claude_cloud.py"
    with subprocess.Popen(
        [sys.executable, "-I", "-S", str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        process.stdout.close()
        process.stdin.write(json.dumps(evidence()).encode())
        process.stdin.close()
        try:
            assert process.wait(timeout=10) == 2
            assert process.stderr.read() == b""
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_incomplete_final_result_may_clear_an_earlier_snapshot_but_still_stops():
    item = evidence()
    audit = audit_messages(
        [
            {
                "type": "result",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
            {"type": "result", "usage": {}},
        ]
    )
    assert audit["result"]["usage_observations"] == 1
    assert audit["result"]["last_usage_observation"] is None
    item["report"]["observations"][0]["report"] = audit
    item["report"]["history_events"] = 2
    item["report"]["sequence"]["numeric"] = 2
    result = evaluate(item)
    assert result["status"] == "stop" and "usage_requires_review" in result["reasons"]
    assert result["report"]["observations"][0]["report"] == audit


def structured_evidence():
    from scripts.claude_cloud_structure import empty_structure, observe_structure

    value = evidence()
    value["report"]["format_version"] = 2
    shape = empty_structure()
    observe_structure(shape, {"data": {"type": "assistant", "message": {"usage": {}}}})
    value["report"]["structure"] = shape
    return value


def test_v2_structure_is_validated_without_promoting_acceptance():
    value = structured_evidence()
    result = evaluate(value)
    assert result["status"] == "sample_observed"
    assert result["report"]["structure"] == value["report"]["structure"]
    assert result["live_acceptance_passed"] is result["ready_for_ingestion"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_location",
        "unknown_type",
        "unknown_category",
        "unknown_field",
        "negative",
        "boolean",
        "sum",
        "field_bound",
        "child_presence",
        "wrong_total",
        "v1_extra",
        "v2_missing",
    ],
)
def test_malformed_structure_cannot_leave_the_offline_validator(mutation):
    value = structured_evidence()
    report = value["report"]
    row = report["structure"]["locations"]["event"]
    if mutation == "unknown_location":
        report["structure"]["locations"]["private-location"] = row
    if mutation == "unknown_type":
        row["types"]["private-type"] = 0
    if mutation == "unknown_category":
        row["categories"]["private-category"] = 0
    if mutation == "unknown_field":
        row["fields"]["private-field"] = 0
    if mutation == "negative":
        row["types"]["object"] = -1
    if mutation == "boolean":
        row["types"]["object"] = True
    if mutation == "sum":
        row["types"]["object"] = 2
    if mutation == "field_bound":
        row["fields"]["usage"] = 2
    if mutation == "child_presence":
        row["fields"]["data"] = 0
    if mutation == "wrong_total":
        report["structure"]["events"] = 0
    if mutation == "v1_extra":
        report["format_version"] = 1
    if mutation == "v2_missing":
        del report["structure"]
    with pytest.raises(EvidenceError, match="invalid_evidence"):
        evaluate(value)


def test_v2_cli_rejects_unknown_structural_keys_without_echoing_them():
    value = structured_evidence()
    value["report"]["structure"]["locations"]["event"]["fields"]["private-transcript-key"] = 1
    script = Path(__file__).resolve().parents[2] / "scripts/verify_claude_cloud.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        input=json.dumps(value).encode(),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2 and result.stderr == b""
    assert json.loads(result.stdout)["reasons"] == ["invalid_evidence"]
    assert b"private-transcript-key" not in result.stdout


@pytest.mark.parametrize("version", [1, 2])
def test_payload_observation_allowed_only_in_explicit_v2(version):
    value = structured_evidence() if version == 2 else evidence()
    value["report"]["observations"][0]["location"] = "payload"
    if version == 1:
        with pytest.raises(EvidenceError):
            evaluate(value)
    else:
        result = evaluate(value)
        assert result["status"] == "sample_observed"
        assert result["ready_for_ingestion"] is False


def test_four_independent_v2_locations_are_not_combined():
    value = structured_evidence()
    original = value["report"]["observations"][0]
    value["report"]["observations"] = [
        {"location": location, "report": copy.deepcopy(original["report"])}
        for location in ("event", "data", "content", "payload")
    ]
    result = evaluate(value)
    assert result["status"] == "stop"
    assert "usage_requires_review" in result["reasons"]
