"""Run the Node `node:test` suite for frontend/js/utils/html.js.

The pure-JS tests live in tests/frontend/test_html.mjs. This wrapper just
shells out to `node --test` so pytest (and therefore `make test` / CI)
catches regressions in the XSS-sensitive escape helpers.

Skipped — not failed — when Node isn't installed locally, so contributors
who only edit Python aren't blocked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JS_TEST_DIR = _REPO_ROOT / "tests" / "frontend"


def test_frontend_html_helpers_pass_node_tests() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node binary not installed; frontend escape tests skipped")

    test_files = sorted(_JS_TEST_DIR.glob("test_*.mjs"))
    if not test_files:
        pytest.skip(f"no .mjs tests found in {_JS_TEST_DIR}")

    result = subprocess.run(
        [node, "--test", *[str(f) for f in test_files]],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "Frontend escape tests failed.\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}",
            pytrace=False,
        )
