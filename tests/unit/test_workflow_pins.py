"""A wrong action pin is a broken pin, not a pinned one."""

import json
from pathlib import Path

import pytest

from scripts.check_workflow_pins import (
    PINS_FILE,
    PINS_LABEL,
    REVISION,
    WORKFLOWS,
    findings,
    load_pins,
    references,
)

GOOD_SHA = "a" * 40
OTHER_SHA = "b" * 40


def write_workflow(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body)


class TestShippedWorkflowsArePinned:
    def test_the_workflows_and_the_record_agree(self):
        assert findings(WORKFLOWS, load_pins()) == []

    def test_every_external_reference_is_pinned_to_a_commit_sha(self):
        for reference in references(WORKFLOWS):
            assert REVISION.fullmatch(reference.revision), (
                f"{reference.path}:{reference.line} pins {reference.action} to "
                f"{reference.revision!r}, which is not a commit SHA"
            )


class TestTheRecord:
    def test_every_recorded_revision_is_a_commit_sha(self):
        for action, revision in load_pins().items():
            assert REVISION.fullmatch(revision), (
                f"{PINS_LABEL} records {revision!r} for {action}, which is not a commit SHA"
            )

    def test_the_record_covers_every_action_the_workflows_use(self):
        recorded = set(load_pins())
        used = {reference.action for reference in references(WORKFLOWS)}
        assert used <= recorded

    def test_the_record_holds_no_action_the_workflows_dropped(self):
        recorded = set(load_pins())
        used = {reference.action for reference in references(WORKFLOWS)}
        assert recorded == used

    def test_the_record_is_readable_json(self):
        assert json.loads(PINS_FILE.read_text())


class TestFindings:
    def test_a_revision_the_record_does_not_carry_is_reported(self, tmp_path):
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      - uses: actions/setup-python@{OTHER_SHA} # v7\n",
        )
        problems = findings(tmp_path, {"actions/setup-python": GOOD_SHA})
        assert len(problems) == 1
        assert "is pinned to" in problems[0]
        assert OTHER_SHA in problems[0]
        assert GOOD_SHA in problems[0]

    def test_a_wellformed_revision_that_does_not_exist_is_reported(self, tmp_path):
        """The shape check alone passed the corrupted setup-python pin."""
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      - uses: actions/setup-python@{OTHER_SHA}\n",
        )
        assert findings(tmp_path, {"actions/setup-python": GOOD_SHA}) != []

    def test_an_unrecorded_action_is_reported(self, tmp_path):
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      - uses: actions/checkout@{GOOD_SHA}\n",
        )
        problems = findings(tmp_path, {"actions/setup-python": GOOD_SHA})
        assert any("actions/checkout is not recorded" in problem for problem in problems)

    def test_a_recorded_action_no_workflow_uses_is_reported(self, tmp_path):
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      - uses: actions/checkout@{GOOD_SHA}\n",
        )
        problems = findings(
            tmp_path, {"actions/checkout": GOOD_SHA, "actions/setup-node": GOOD_SHA}
        )
        assert len(problems) == 1
        assert "no workflow uses it" in problems[0]

    def test_a_mutable_reference_is_reported(self, tmp_path):
        write_workflow(
            tmp_path,
            "ci.yml",
            "      - uses: actions/checkout@v7\n",
        )
        problems = findings(tmp_path, {"actions/checkout": GOOD_SHA})
        assert len(problems) == 1
        assert "not pinned to a commit SHA" in problems[0]

    def test_a_local_workflow_path_needs_no_revision(self, tmp_path):
        write_workflow(
            tmp_path,
            "ci.yml",
            "      - uses: ./.github/workflows/other.yml\n",
        )
        assert findings(tmp_path, {}) == []

    def test_a_comment_after_the_reference_is_ignored(self, tmp_path):
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      - uses: actions/checkout@{GOOD_SHA} # v7\n",
        )
        assert findings(tmp_path, {"actions/checkout": GOOD_SHA}) == []


class TestLoadPins:
    def test_a_missing_record_is_reported(self, tmp_path):
        with pytest.raises(ValueError, match="missing"):
            load_pins(tmp_path / "action_pins.json")

    def test_a_malformed_record_is_reported(self, tmp_path):
        broken = tmp_path / "action_pins.json"
        broken.write_text("{not json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_pins(broken)

    def test_a_record_that_is_not_a_mapping_is_reported(self, tmp_path):
        wrong = tmp_path / "action_pins.json"
        wrong.write_text("[]")
        with pytest.raises(ValueError, match="must map an action"):
            load_pins(wrong)
