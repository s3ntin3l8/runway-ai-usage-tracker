"""Unit tests for from-source edge-version detection in scripts/sidecar.py.

A from-source checkout self-classifies as edge when it sits past the latest
release tag — `_from_source_edge_suffix` parses `git describe` to decide.
"""

from unittest.mock import MagicMock, patch

import scripts.sidecar as sidecar


def _git(stdout: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


class TestFromSourceEdgeSuffix:
    def test_on_clean_release_tag_is_stable(self):
        with patch.object(sidecar.subprocess, "run", return_value=_git("v1.1.0-0-gabcdef1\n")):
            assert sidecar._from_source_edge_suffix() == ""

    def test_dirty_on_release_tag_is_edge(self):
        with patch.object(
            sidecar.subprocess, "run", return_value=_git("v1.1.0-0-gabcdef1-dirty\n")
        ):
            assert sidecar._from_source_edge_suffix() == "+edge.abcdef1"

    def test_ahead_of_tag_is_edge(self):
        with patch.object(sidecar.subprocess, "run", return_value=_git("v1.1.0-11-g28075ab4\n")):
            assert sidecar._from_source_edge_suffix() == "+edge.28075ab4"

    def test_no_release_tag_bare_sha_is_edge(self):
        with patch.object(sidecar.subprocess, "run", return_value=_git("abcdef123456\n")):
            assert sidecar._from_source_edge_suffix() == "+edge.abcdef123456"

    def test_no_release_tag_dirty_is_edge(self):
        with patch.object(sidecar.subprocess, "run", return_value=_git("abcdef1-dirty\n")):
            assert sidecar._from_source_edge_suffix() == "+edge.abcdef1"

    def test_git_failure_is_stable(self):
        with patch.object(sidecar.subprocess, "run", return_value=_git("", returncode=128)):
            assert sidecar._from_source_edge_suffix() == ""

    def test_git_missing_is_stable(self):
        with patch.object(sidecar.subprocess, "run", side_effect=OSError("git not found")):
            assert sidecar._from_source_edge_suffix() == ""

    def test_unexpected_output_is_stable(self):
        with patch.object(sidecar.subprocess, "run", return_value=_git("garbage output\n")):
            assert sidecar._from_source_edge_suffix() == ""
