"""A wrong action pin is a broken pin, not a pinned one."""

import json
from pathlib import Path

import pytest

from scripts.check_workflow_pins import (
    DIGEST,
    LOCKFILE,
    PINS_FILE,
    PINS_LABEL,
    PLAYWRIGHT_IMAGE,
    REVISION,
    WORKFLOWS,
    findings,
    images,
    load_pins,
    locked_playwright,
    references,
)

GOOD_SHA = "a" * 40
OTHER_SHA = "b" * 40
GOOD_DIGEST = "sha256:" + "c" * 64


def write_workflow(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body)


def write_lockfile(directory: Path, version: str) -> Path:
    """A lockfile that pins one Playwright version."""
    path = directory / "package-lock.json"
    path.write_text(
        json.dumps({"packages": {"node_modules/playwright-core": {"version": version}}})
    )
    return path


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


class TestContainerImages:
    """A container image moves under the build exactly like a branch does."""

    def test_an_image_pinned_to_a_tag_is_reported(self, tmp_path):
        write_workflow(
            tmp_path,
            "ci.yml",
            "      image: mcr.microsoft.com/playwright:v1.64.1-noble\n",
        )
        problems = findings(tmp_path, {})
        assert len(problems) == 1
        assert "not pinned to a digest" in problems[0]

    def test_a_digest_pinned_image_is_accepted(self, tmp_path):
        lockfile = write_lockfile(tmp_path, "1.64.1")
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      image: mcr.microsoft.com/playwright:v1.64.1-noble@{GOOD_DIGEST}\n",
        )
        assert findings(tmp_path, {}, lockfile) == []

    def test_a_service_image_is_covered_too(self, tmp_path):
        write_workflow(
            tmp_path,
            "ci.yml",
            "    services:\n      nginx:\n        image: nginx:latest\n",
        )
        problems = findings(tmp_path, {})
        assert len(problems) == 1
        assert "not pinned to a digest" in problems[0]

    def test_the_playwright_image_must_match_the_locked_version(self, tmp_path):
        """The image carries the browsers; the lockfile pins the version."""
        lockfile = write_lockfile(tmp_path, "1.63.0")
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      image: mcr.microsoft.com/playwright:v1.64.1-noble@{GOOD_DIGEST}\n",
        )
        problems = findings(tmp_path, {}, lockfile)
        assert len(problems) == 1
        assert "lockfile pins playwright-core at 1.63.0" in problems[0]

    def test_the_playwright_image_at_the_locked_version_is_accepted(self, tmp_path):
        lockfile = write_lockfile(tmp_path, "1.64.1")
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      image: mcr.microsoft.com/playwright:v1.64.1-noble@{GOOD_DIGEST}\n",
        )
        assert findings(tmp_path, {}, lockfile) == []

    def test_a_playwright_image_without_a_locked_version_is_reported(self, tmp_path):
        """A check that cannot run is not a check: it has to say so."""
        write_workflow(
            tmp_path,
            "ci.yml",
            f"      image: mcr.microsoft.com/playwright:v1.64.1-noble@{GOOD_DIGEST}\n",
        )
        problems = findings(tmp_path, {}, tmp_path / "missing.json")
        assert len(problems) == 1
        assert "the lockfile pins no version" in problems[0]

    def test_the_shipped_images_are_digest_pinned(self):
        for image in images(WORKFLOWS):
            assert DIGEST.fullmatch(image.digest), (
                f"{image.path}:{image.line} runs {image.reference}, which is not pinned to a digest"
            )

    def test_the_shipped_playwright_image_matches_the_locked_version(self):
        """The image carries the browsers; the lockfile pins the version."""
        locked = locked_playwright(LOCKFILE)
        assert locked is not None, "the lockfile pins no Playwright version"
        shipped = [image for image in images(WORKFLOWS) if image.repository == PLAYWRIGHT_IMAGE]
        assert shipped, "no workflow runs the Playwright image"
        for image in shipped:
            assert image.version == locked, (
                f"{image.path}:{image.line} runs {image.reference}, but the "
                f"lockfile pins playwright-core at {locked}"
            )


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
