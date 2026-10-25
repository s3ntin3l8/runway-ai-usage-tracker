"""The offline diagnostic envelope accepts fixed categories, never raw page data."""

import copy

import pytest

from app.models.claude_cloud_list_boundary import ListBoundaryReport
from app.services.claude_cloud_list_boundary import _empty
from scripts.claude_cloud_list_boundary import observe_list_boundary
from scripts.verify_claude_cloud import EvidenceError, evaluate


def evidence():
    report = _empty()
    report.update(
        outcome="observed",
        stage="complete",
        requests=2,
        http_status=200,
        response_encoding="identity",
        json_state="decoded",
        account_verified=True,
        organization_verified=True,
        original_list_pages=71,
        original_listed_sessions=1420,
        boundary=observe_list_boundary(
            {"data": [{"id": "private-session"}], "next_cursor": "", "has_more": False},
            b"k" * 32,
            set(),
            set(),
        ),
    )
    return {
        "format_version": 3,
        "source_commit": "d" * 40,
        "image": "ghcr.io/s3ntin3l8/runway@sha256:" + "e" * 64,
        "observed_at": "2026-10-25T00:00:00Z",
        "deployment": "isolated-test",
        "report": report,
    }


def test_observed_boundary_roundtrips_but_never_authenticates_the_contract():
    value = evidence()
    ListBoundaryReport.model_validate(value["report"])
    result = evaluate(value)
    assert result["status"] == "stop" and result["report"] == value["report"]
    assert result["cloud_contract_verified"] is False
    assert "list_contract_requires_review" in result["reasons"]


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(job_handle="a" * 32),
        lambda r: r["boundary"].update(next_cursor="private-cursor"),
        lambda r: r["boundary"].update(record_count=True),
        lambda r: r["boundary"].update(cursor_repeated=False),
        lambda r: r["boundary"]["id_kinds"].update(string=3),
        lambda r: r.update(requests=3),
        lambda r: r.update(live_acceptance_passed=True),
    ],
)
def test_unknown_and_inconsistent_diagnostics_are_rejected(change):
    value = copy.deepcopy(evidence())
    change(value["report"])
    with pytest.raises(EvidenceError, match="^invalid_evidence$"):
        evaluate(value)


def test_diagnostic_evidence_runs_as_one_file_without_application_or_site_packages(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(Path("scripts/verify_claude_cloud.py").resolve())],
        input=json.dumps(evidence()),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=10,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["status"] == "stop"


def test_unknown_checkpoint_prefix_stays_null_in_offline_evidence():
    value = evidence()
    value["report"] = _empty()
    result = evaluate(value)
    assert result["report"]["original_list_pages"] is None
    assert result["report"]["original_listed_sessions"] is None
    assert result["status"] == "stop"
