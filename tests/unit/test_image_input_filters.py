"""Every file the Dockerfile copies from scripts/ must trigger the image gates."""

import fnmatch
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
NON_IMAGE_SCRIPTS = (
    "scripts/dev_checks.py",
    "scripts/sidecar_pkg/__init__.py",
    "scripts/sidecar_pkg/credentials.py",
    "scripts/claude_cloud_structure.py.bak",
)


def _copied_script_inputs() -> list[str]:
    inputs: list[str] = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        match = re.match(r"COPY\s+(.+?)\s+\./scripts/(\S*)$", line.strip())
        if match:
            inputs.extend(match.group(1).split())
    return inputs


def _pr_path_globs(name: str) -> list[str]:
    text = (WORKFLOWS / name).read_text()
    block = text.split("paths:", 1)[1].split("\n  schedule:", 1)[0]
    block = block.split("\n  workflow_dispatch:", 1)[0]
    return re.findall(r"^\s*-\s*'([^']+)'", block, re.M)


def _publish_detector() -> re.Pattern[str]:
    text = (WORKFLOWS / "ci-cd.yml").read_text()
    match = re.search(r"grep -qE '([^']+)'; then", text)
    assert match, "image-changed grep not found"
    return re.compile(match.group(1))


def test_dockerfile_declares_script_inputs():
    inputs = _copied_script_inputs()
    assert "scripts/audit_claude_usage.py" in inputs
    assert "scripts/sidecar_pkg/source_catalog_data.py" in inputs
    assert all(i.startswith("scripts/") for i in inputs)


@pytest.mark.parametrize("source", _copied_script_inputs())
def test_publish_detector_matches_copied_script(source):
    assert _publish_detector().search(source)


@pytest.mark.parametrize("source", NON_IMAGE_SCRIPTS)
def test_publish_detector_ignores_non_image_scripts(source):
    assert not _publish_detector().search(source)


@pytest.mark.parametrize("name", ["image-security.yml", "image-security-arm64.yml"])
@pytest.mark.parametrize("source", _copied_script_inputs())
def test_candidate_scans_trigger_on_copied_script(name, source):
    globs = _pr_path_globs(name)
    assert any(fnmatch.fnmatchcase(source, g) for g in globs)


@pytest.mark.parametrize("name", ["image-security.yml", "image-security-arm64.yml"])
@pytest.mark.parametrize("source", NON_IMAGE_SCRIPTS)
def test_candidate_scans_ignore_non_image_scripts(name, source):
    globs = _pr_path_globs(name)
    assert not any(fnmatch.fnmatchcase(source, g) for g in globs)
