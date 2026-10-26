"""A standard-library evaluator never upgrades visibility to accounting acceptance."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.models.claude_chat_fixture import ChatFixtureReport
from scripts.claude_chat_evidence import empty_report, observe_conversation
from scripts.verify_claude_chat_fixture import evaluate

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verify_claude_chat_fixture.py"


def evidence(observed=True):
    report = empty_report()
    if observed:
        report = observe_conversation({"chat_messages": []})
        report.update(
            outcome="observed",
            stage="complete",
            requests=3,
            http_status=200,
            account_verified=True,
            organization_verified=True,
            target_verified=True,
        )
    return {
        "format_version": 1,
        "source_commit": "a" * 40,
        "image": "ghcr.io/s3ntin3l8/runway@sha256:" + "b" * 64,
        "observed_at": "2026-10-26T12:00:00Z",
        "deployment": "isolated-test",
        "report": report,
    }


@pytest.mark.parametrize("visible", [False, True])
def test_valid_reports_preserve_visibility_but_always_stop_acceptance(visible):
    body = evidence(visible)
    ChatFixtureReport.model_validate(body["report"])
    result = evaluate(body)
    assert result["status"] == "stop" and result["fixture_visible"] is visible
    assert result["ready_for_ingestion"] is False
    assert result["report"] == body["report"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("history_complete", True),
        ("ready_for_ingestion", True),
        ("format_version", True),
        ("requests", True),
        ("requests", 4),
        ("source_id", "private-id"),
        ("messages", 1),
        ("http_status", 403),
    ],
)
def test_false_evidence_is_rejected_by_both_models(field, value):
    body = evidence()
    body["report"][field] = value
    with pytest.raises(ValueError):
        evaluate(body)
    with pytest.raises(ValueError):
        ChatFixtureReport.model_validate(body["report"])


def test_unknown_counter_keys_and_inconsistent_histograms_are_rejected():
    body = evidence()
    body["report"]["usage"]["conversation"]["fields"]["input_tokens"]["zero"] = 1
    with pytest.raises(ValueError):
        evaluate(body)
    body = evidence()
    body["report"]["roles"]["private-id"] = 0
    with pytest.raises(ValueError):
        evaluate(body)


def test_evaluator_runs_isolated_without_application_or_environment_dependencies():
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT)],
        input=json.dumps(evidence()).encode(),
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout) == evaluate(evidence())
    assert not result.stderr


@pytest.mark.parametrize(
    "raw",
    [
        b'{"report":"private-cookie","report":"private-id"}',
        b'{"source_commit":NaN}',
        b"invalid-private-id",
        b"x" * (64 * 1024 + 1),
    ],
)
def test_malformed_inputs_never_echo_body_or_exception(raw):
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT)], input=raw, capture_output=True, timeout=5
    )
    assert result.returncode == 2 and not result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "stop" and report["fixture_visible"] is False
    assert b"private" not in result.stdout


def test_producer_histograms_and_verifier_cover_all_observed_counter_shapes():
    values = [None, False, 0, 3, -1, 1.5, 2**63, "private", [], {}]
    for value in values:
        body = evidence()
        report = observe_conversation(
            {
                "usage": {"input_tokens": value},
                "chat_messages": [
                    {"sender": "assistant", "usage": {"input_tokens": value}, "token_count": value}
                ],
            }
        )
        report.update(
            outcome="observed",
            stage="complete",
            requests=3,
            http_status=200,
            account_verified=True,
            organization_verified=True,
            target_verified=True,
        )
        body["report"] = report
        ChatFixtureReport.model_validate(report)
        assert evaluate(body)["fixture_visible"] is True
        assert "private" not in json.dumps(evaluate(body))


def test_runtime_image_includes_chat_helpers():
    root = SCRIPT.parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    exclusions = (root / ".dockerignore").read_text()
    for filename in ("claude_chat_evidence.py", "verify_claude_chat_fixture.py"):
        assert f"COPY scripts/{filename} ./scripts/" in dockerfile
        assert f"!scripts/{filename}" in exclusions


@pytest.mark.parametrize(
    "changes",
    [
        {"format_version": True},
        {"source_commit": "private-identifier"},
        {"image": "https://outside.invalid/private"},
        {"observed_at": "private"},
        {"observed_at": "2026-02-31T12:00:00Z"},
        {"deployment": "private"},
    ],
)
def test_envelope_requires_immutable_and_well_formed_context(changes):
    with pytest.raises(ValueError):
        evaluate(evidence() | changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("roles", {"human": 1, "assistant": 0, "unknown": 0}),
        ("identities", {"present": 1, "missing": 0, "invalid": 0, "duplicate": 0}),
        ("identities", {"present": 0, "missing": 0, "invalid": 0, "duplicate": 1}),
        ("requests", 0),
        ("http_status", 999),
        ("account_verified", 1),
        ("outcome", "private"),
        ("stage", "private"),
    ],
)
def test_invalid_evidence_counts_and_types_are_rejected(field, value):
    body = evidence()
    body["report"][field] = value
    with pytest.raises(ValueError):
        evaluate(body)
    with pytest.raises(ValueError):
        ChatFixtureReport.model_validate(body["report"])


def test_uncertain_authority_cannot_retain_partial_identity_or_observations():
    for changes in (
        {"outcome": "source_changed", "stage": "account", "account_verified": True, "requests": 1},
        {"organization_verified": True, "requests": 2},
        {"account_verified": True, "requests": 0},
        {"stage": "complete"},
        {"target_verified": True},
    ):
        body = evidence(False)
        body["report"].update(changes)
        with pytest.raises(ValueError):
            evaluate(body)
        with pytest.raises(ValueError):
            ChatFixtureReport.model_validate(body["report"])


@pytest.mark.parametrize("raw", [None, b"invalid-private", b"x" * (64 * 1024 + 1)])
def test_cli_entrypoint_in_process_retains_only_sanitized_decision(monkeypatch, capsys, raw):
    import io
    from types import SimpleNamespace

    from scripts import verify_claude_chat_fixture as verifier

    data = json.dumps(evidence()).encode() if raw is None else raw
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
    assert verifier.main() == 2
    output = capsys.readouterr()
    assert not output.err and "private" not in output.out
    assert json.loads(output.out)["status"] == "stop"
