"""Complete-chain evidence is separate from bounded samples and never acceptance."""

import copy

import pytest

from app.models.claude_cloud_verification import VerificationState
from app.services.claude_cloud_verification import build_verification_report
from scripts.verify_claude_cloud import EvidenceError, evaluate


def evidence():
    state = VerificationState(
        account_id="private-holder",
        source_id="private-source",
        job_key="a" * 64,
        source_digest="b" * 64,
        created_at=1000.0,
        deadline=2800.0,
    )
    report = build_verification_report("c" * 32, state)
    report.pop("job_handle")
    return {
        "format_version": 2,
        "source_commit": "d" * 40,
        "image": "ghcr.io/s3ntin3l8/runway@sha256:" + "e" * 64,
        "observed_at": "2026-10-25T00:00:00Z",
        "deployment": "isolated-test",
        "report": report,
    }


def test_new_envelope_roundtrips_without_claiming_acceptance():
    result = evaluate(evidence())
    assert result["status"] == "stop"
    assert "history_not_complete" in result["reasons"]
    assert result["report"] == evidence()["report"]
    assert result["live_acceptance_passed"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(job_handle="c" * 32),
        lambda r: r.update(account_id="private-holder"),
        lambda r: r["audit"].update(secret="private-fixture"),
        lambda r: r["scope_observations"].update(record_versions=["private-fixture"]),
        lambda r: r.update(requests=True),
        lambda r: r.update(live_acceptance_passed=True),
        lambda r: r.update(deadline=10000),
        lambda r: r.update(history_events=1),
        lambda r: r["comparison"].update(all_expected_input_cache_match=True),
    ],
)
def test_rejects_identifiers_and_inconsistent_or_unallowlisted_data(mutate):
    value = copy.deepcopy(evidence())
    mutate(value["report"])
    with pytest.raises(EvidenceError, match="^invalid_evidence$"):
        evaluate(value)


def test_public_model_rejects_nested_private_metadata():
    from pydantic import ValidationError

    from app.models.claude_cloud_verification import VerificationReport

    report = {**evidence()["report"], "job_handle": "c" * 32}
    assert VerificationReport.model_validate(report).job_handle == "c" * 32
    report["audit"]["assistant"]["message_id"] = "private-fixture"
    with pytest.raises(ValidationError):
        VerificationReport.model_validate(report)


def test_complete_protocol_evidence_runs_without_site_packages(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(Path("scripts/verify_claude_cloud.py").resolve())],
        input=json.dumps(evidence()),
        text=True,
        capture_output=True,
        timeout=10,
        cwd=tmp_path,
    )
    assert completed.returncode == 2  # Valid evidence still requires acceptance review.
    result = json.loads(completed.stdout)
    assert result["status"] == "stop"
    assert result["live_acceptance_passed"] is False


def test_runtime_image_includes_standalone_report_validator():
    from pathlib import Path

    # Report validation imports this stdlib-only module in the production image.
    dockerfile = Path("Dockerfile").read_text()
    assert "COPY scripts/verify_claude_cloud.py ./scripts/" in dockerfile
    assert "!scripts/verify_claude_cloud.py" in Path(".dockerignore").read_text()
