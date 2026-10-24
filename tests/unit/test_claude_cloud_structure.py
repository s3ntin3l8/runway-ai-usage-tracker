"""Fixed structural evidence never returns provider text, keys or identifiers."""

import copy
import json


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
