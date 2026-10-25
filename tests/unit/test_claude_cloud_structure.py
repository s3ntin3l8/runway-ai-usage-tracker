"""Fixed structural evidence never returns provider text, keys or identifiers."""

import copy
import json

import pytest


def test_declared_paths_describe_wrappers_without_returning_contents():
    from scripts.claude_cloud_structure import empty_structure, observe_structure

    event = {
        "type": "envelope-private",
        "private-key": "private-value",
        "data": {"type": "assistant", "message": {"usage": {"private-counter": 7}}},
        "content": '{"type":"assistant","private":"transcript"}',
        "message": ["private-array-text", {"type": "assistant"}],
    }
    before = copy.deepcopy(event)
    report = empty_structure()
    observe_structure(report, event)
    assert report["events"] == 1
    rows = report["locations"]
    assert rows["event"]["types"]["object"] == 1
    assert rows["event"]["categories"]["other"] == 1
    assert rows["data"]["categories"]["assistant"] == 1
    assert rows["data.message"]["fields"]["usage"] == 1
    assert rows["data.message"]["categories"]["missing"] == 1
    assert rows["content"]["types"]["string"] == 1
    assert rows["content"]["categories"]["not_object"] == 1
    assert rows["content.message"]["types"]["missing"] == 1
    assert rows["message"]["types"]["array"] == 1
    assert rows["payload"]["types"]["missing"] == 1
    for secret in (
        "envelope-private",
        "private-key",
        "private-value",
        "private-counter",
        "transcript",
    ):
        assert secret not in json.dumps(report)
    assert event == before


def test_scalar_types_and_categories_are_separate_and_counts_are_per_event():
    from scripts.claude_cloud_structure import empty_structure, observe_structure

    report = empty_structure()
    for value in (None, False, 3, [], "private-text", {"type": "result"}, {"type": 17}):
        observe_structure(report, {"data": value})
    row = report["locations"]["data"]
    assert report["events"] == 7
    assert row["types"]["null"] == row["types"]["boolean"] == row["types"]["number"] == 1
    assert row["categories"]["result"] == row["categories"]["non_string"] == 1
    assert row["categories"]["not_object"] == 5
    for location in report["locations"].values():
        assert sum(location["types"].values()) == sum(location["categories"].values()) == 7
    assert "private-text" not in json.dumps(report)


def test_empty_reports_do_not_share_mutable_counters():
    from scripts.claude_cloud_structure import empty_structure, observe_structure

    first, second = empty_structure(), empty_structure()
    observe_structure(first, {"type": "assistant"})
    assert second["events"] == 0
    assert all(not any(row["types"].values()) for row in second["locations"].values())


def test_provider_type_strings_cannot_spoof_reserved_meta_categories():
    from scripts.claude_cloud_structure import empty_structure, observe_structure

    report = empty_structure()
    for category in ("missing", "non_string", "not_object", "other"):
        observe_structure(report, {"type": category})
    row = report["locations"]["event"]
    assert row["categories"]["other"] == 4
    assert row["categories"]["missing"] == row["categories"]["not_object"] == 0
    assert row["fields"]["type"] == 4


@pytest.mark.parametrize(
    "mutation",
    [
        "location",
        "type_key",
        "negative",
        "large",
        "boolean",
        "sum",
        "field_bound",
        "parent",
        "category",
    ],
)
def test_api_model_rejects_incomplete_or_inconsistent_histograms(mutation):
    from pydantic import ValidationError

    from app.models.claude_cloud_probe import CloudStructure
    from scripts.claude_cloud_structure import empty_structure, observe_structure

    value = empty_structure()
    observe_structure(value, {"data": {"type": "assistant"}})
    assert CloudStructure.model_validate(value).events == 1
    row = value["locations"]["event"]
    if mutation == "location":
        del value["locations"]["payload"]
    if mutation == "type_key":
        del row["types"]["string"]
    if mutation == "negative":
        row["types"]["object"] = -5
    if mutation == "large":
        row["types"]["string"] = 999
    if mutation == "boolean":
        row["types"]["object"] = True
    if mutation == "sum":
        row["types"]["string"] = 1
    if mutation == "field_bound":
        value["locations"]["payload"]["fields"]["usage"] = 1
    if mutation == "parent":
        row["fields"]["data"] = 0
    if mutation == "category":
        row["categories"]["not_object"] = 1
    with pytest.raises(ValidationError):
        CloudStructure.model_validate(value)


def test_api_v2_history_and_structure_event_counts_must_match():
    from pydantic import ValidationError

    from app.models.claude_cloud_probe import ClaudeCloudProbeReportV2
    from scripts.claude_cloud_structure import empty_structure

    value = {
        "outcome": "source_unavailable",
        "stage": "source",
        "sequence": dict.fromkeys(("numeric", "invalid", "duplicates", "gaps", "out_of_order"), 0),
        "structure": empty_structure(),
        "history_events": 1,
    }
    with pytest.raises(ValidationError):
        ClaudeCloudProbeReportV2.model_validate(value)


def test_v2_model_preserves_payload_observation_and_v1_rejects_it():
    from pydantic import ValidationError

    from app.models.claude_cloud_probe import ClaudeCloudProbeReport, ClaudeCloudProbeReportV2
    from app.services.claude_cloud_probe import _empty_report

    report = _empty_report(2)
    report["observations"] = [{"location": "payload", "report": {}}]
    validated = ClaudeCloudProbeReportV2.model_validate(report)
    assert validated.model_dump()["observations"][0]["location"] == "payload"
    report.pop("structure")
    report["format_version"] = 1
    with pytest.raises(ValidationError):
        ClaudeCloudProbeReport.model_validate(report)
