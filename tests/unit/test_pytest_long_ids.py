"""An oversized auto-generated test id fails at collection, on every platform.

pytest keeps the node id in ``PYTEST_CURRENT_TEST`` and Windows caps an environment variable
at 32767 characters, so a parametrized str/bytes value of tens of thousands of characters
used to error only on the native Windows suite. ``parametrize_long_str_id_strategy =
disallow`` in ``pytest.ini`` turns it into a collection error everywhere.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _collect(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    case = tmp_path / "test_case.py"
    case.write_text(source, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(ROOT / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            str(case),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )


def test_an_oversized_auto_generated_id_fails_at_collection(tmp_path):
    result = _collect(
        tmp_path,
        "import pytest\n\n"
        '@pytest.mark.parametrize("body", ["x" * 20000])\n'
        "def test_case(body):\n"
        "    pass\n",
    )
    assert result.returncode != 0
    assert "too long for an auto-generated ID" in result.stdout + result.stderr


def test_an_explicit_id_keeps_an_oversized_value_collectable(tmp_path):
    result = _collect(
        tmp_path,
        "import pytest\n\n"
        '@pytest.mark.parametrize("body", [pytest.param("x" * 20000, id="oversized")])\n'
        "def test_case(body):\n"
        "    pass\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_case[oversized]" in result.stdout


def test_the_limit_is_one_hundred_characters(tmp_path):
    source = (
        "import pytest\n\n"
        '@pytest.mark.parametrize("body", ["x" * {length}])\n'
        "def test_case(body):\n"
        "    pass\n"
    )
    at_limit = _collect(tmp_path, source.format(length=100))
    assert at_limit.returncode == 0, at_limit.stdout + at_limit.stderr
    over_limit = _collect(tmp_path, source.format(length=101))
    assert over_limit.returncode != 0
    assert "too long for an auto-generated ID" in over_limit.stdout + over_limit.stderr
