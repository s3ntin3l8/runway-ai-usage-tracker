"""Producer and independent verifier agree on content-free partial evidence."""

import copy

import pytest
from pydantic import ValidationError

from app.models.claude_chat_fixture import ChatFixtureReport, ChatFixtureRequest
from scripts.claude_chat_evidence import empty_report, observe_conversation

ORG = "11111111-1111-4111-8111-111111111111"
TARGET = "33333333-3333-4333-8333-333333333333"


def observed():
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
    return report


def test_request_forbids_unknown_fields_and_implicit_targets():
    body = {
        "account_id": "fixture@example.invalid",
        "source_id": "fixture",
        "organization_id": ORG,
        "conversation_id": TARGET,
    }
    assert ChatFixtureRequest.model_validate(body).conversation_id == TARGET
    for change in (
        {"conversation_id": "../private"},
        {"organization_id": None},
        {"account_id": False},
        {"url": "https://outside.invalid"},
    ):
        with pytest.raises(ValidationError):
            ChatFixtureRequest.model_validate(body | change)


def test_empty_and_observed_reports_round_trip_without_completeness_claims():
    for report in [empty_report(), observed()]:
        assert ChatFixtureReport.model_validate(report).model_dump() == report


@pytest.mark.parametrize(
    "field,value",
    [
        ("history_complete", True),
        ("history_complete", 0),
        ("ready_for_ingestion", True),
        ("provenance_verified", True),
        ("cloud_contract_verified", True),
        ("exact_token_accounting_verified", True),
        ("format_version", True),
        ("requests", True),
        ("requests", 4),
        ("account_verified", 1),
        ("messages", -1),
        ("messages", 501),
        ("unknown", "private-identifier"),
    ],
)
def test_strict_reports_reject_nonliteral_or_unsupported_claims(field, value):
    with pytest.raises(ValidationError):
        ChatFixtureReport.model_validate(observed() | {field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"requests": 2},
        {"http_status": 403},
        {"target_verified": False},
        {"account_verified": False},
        {"organization_verified": False},
        {"stage": "source"},
        {"outcome": "rejected"},
    ],
)
def test_observed_requires_complete_identity_and_target_chain(change):
    with pytest.raises(ValidationError):
        ChatFixtureReport.model_validate(observed() | change)


def test_histogram_counts_and_key_sets_are_not_assertions_from_the_operator():
    report = observed()
    report["roles"]["assistant"] = 1
    with pytest.raises(ValidationError):
        ChatFixtureReport.model_validate(report)
    report = observed()
    report["usage"]["conversation"]["fields"]["input_tokens"]["zero"] = 1
    with pytest.raises(ValidationError):
        ChatFixtureReport.model_validate(report)
    report = observed()
    report["usage"]["messages"]["fields"]["secret"] = copy.deepcopy(
        report["usage"]["messages"]["fields"]["input_tokens"]
    )
    with pytest.raises(ValidationError):
        ChatFixtureReport.model_validate(report)
