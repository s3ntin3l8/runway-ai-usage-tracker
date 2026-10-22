"""The test suite's per-worker isolation, pinned.

The split has to happen in ``tests/conftest.py`` at import time -- before the
app is imported and its settings singleton reads the paths -- so a fixture
cannot pin it. These tests reach it the only two ways that work: the pure
path derivation, and a fresh interpreter that imports the conftest with a
worker id set and reports the paths it ended up with.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.isolation import worker_paths

ROOT = Path(__file__).resolve().parents[2]


def test_worker_paths_differ_per_worker(tmp_path):
    serial_home, serial_config = worker_paths(tmp_path, "")
    assert serial_home == tmp_path / "home"
    assert serial_config == tmp_path / "config"

    worker_home, worker_config = worker_paths(tmp_path, "gw3")
    assert worker_home == tmp_path / "home-gw3"
    assert worker_config == tmp_path / "config-gw3"


def test_the_conftest_applies_the_split_to_a_fresh_interpreter(tmp_path):
    """A worker gets its own home and config root.

    The subprocess is what makes this a real pin: replacing the
    ``if _isolation_root:`` guard in the conftest with ``if False:`` leaves
    every in-process test green, because the conftest has already run by then.
    """
    env = {
        **os.environ,
        "TEST_ISOLATION_ROOT": str(tmp_path),
        "PYTEST_XDIST_WORKER": "gw9",
    }
    code = (
        "import os, tests.conftest;print(os.environ['RUNWAY_CONFIG_DIR']);print(os.environ['HOME'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    config, home = result.stdout.split()
    assert config == str(tmp_path / "config-gw9")
    assert home == str(tmp_path / "home-gw9")
    assert (tmp_path / "config-gw9").is_dir()
    assert (tmp_path / "home-gw9").is_dir()


def test_a_serial_run_uses_the_shared_root(tmp_path):
    """With no worker id there is one home and one config root."""
    env = {**os.environ, "TEST_ISOLATION_ROOT": str(tmp_path)}
    env.pop("PYTEST_XDIST_WORKER", None)
    code = (
        "import os, tests.conftest;print(os.environ['RUNWAY_CONFIG_DIR']);print(os.environ['HOME'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    config, home = result.stdout.split()
    assert config == str(tmp_path / "config")
    assert home == str(tmp_path / "home")


@pytest.mark.skipif(
    not (os.environ.get("PYTEST_XDIST_WORKER") and os.environ.get("TEST_ISOLATION_ROOT")),
    reason="only meaningful in a parallel worker of an isolated run",
)
def test_this_worker_is_running_in_its_own_root():
    """Under xdist, the paths in use are this worker's."""
    worker = os.environ["PYTEST_XDIST_WORKER"]
    assert os.environ["RUNWAY_CONFIG_DIR"].endswith(f"config-{worker}")
    assert os.environ["HOME"].endswith(f"home-{worker}")
