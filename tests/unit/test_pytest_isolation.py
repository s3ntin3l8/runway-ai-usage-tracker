"""A plain ``pytest`` run must not reach the operator's real home directory."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = textwrap.dedent(
    """
    import os
    from pathlib import Path

    from tests.fixtures.isolation import LIVE_STATE_ENV_VARS

    def test_probe():
        root = Path(os.environ["TEST_ISOLATION_ROOT"]).resolve()
        # State paths exported in the operator's shell must not survive.
        assert not [n for n in LIVE_STATE_ENV_VARS if n in os.environ]
        for value in (
            Path.home(),
            Path(os.environ["RUNWAY_CONFIG_DIR"]),
            Path(os.environ["XDG_CONFIG_HOME"]),
            Path(os.environ["APPDATA"]),
        ):
            assert root in value.resolve().parents, value
        Path(os.environ["PROBE_OUT"]).write_text(str(root), encoding="utf-8")
    """
)


def _run_probe(tmp_path, *, extra_args=(), extra_env=None, probe_source=_PROBE):
    """Run ``probe_source`` under a bare pytest (no runner-exported root)."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir(exist_ok=True)
    probe = probe_dir / "test_probe.py"
    probe.write_text(probe_source, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "TEST_ISOLATION_ROOT"}
    env["HOME"] = env["USERPROFILE"] = str(real_home)
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp_path)
    env["PROBE_OUT"] = str(tmp_path / "root.txt")
    env.pop("RUNWAY_CONFIG_DIR", None)
    env.pop("PYTEST_XDIST_WORKER", None)
    env.update(extra_env or {})
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT), str(probe_dir), env.get("PYTHONPATH")])
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-c",
            str(REPO_ROOT / "pytest.ini"),
            "--rootdir",
            str(probe_dir),
            "-p",
            "tests.conftest",
            *extra_args,
            "-p",
            "no:cacheprovider",
            "-s",
            "-q",
            "--no-cov",
            "--basetemp",
            str(tmp_path / "basetemp"),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return result, real_home


def test_a_bare_pytest_run_is_isolated_and_cleans_up(tmp_path):
    """No ``TEST_ISOLATION_ROOT`` from a runner: conftest makes and removes one."""
    result, real_home = _run_probe(
        tmp_path,
        extra_env={
            "DATABASE_PATH": str(tmp_path / "live.db"),
            "HERMES_HOME": str(tmp_path / "live-hermes"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "live-claude"),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    root = Path((tmp_path / "root.txt").read_text(encoding="utf-8"))
    assert root.name.startswith("runway-pytest-")
    assert root.parent in (tmp_path, tmp_path.resolve())
    # The temp root is removed at exit, and the "real" home was never written.
    assert not root.exists()
    assert list(real_home.iterdir()) == []


_REEXPORT_HOME_PLUGIN = textwrap.dedent(
    """
    import os

    def pytest_sessionstart(session):
        # A plugin that undoes the isolation after conftest installed it.
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.environ["REAL_HOME"]
    """
)


def test_the_guard_aborts_when_home_escapes_the_isolation_root(tmp_path):
    """The guard is the safety net: it must fail the run, not just exist."""
    (tmp_path / "probe").mkdir()
    (tmp_path / "probe" / "reexport_home.py").write_text(_REEXPORT_HOME_PLUGIN, encoding="utf-8")
    result, _ = _run_probe(
        tmp_path,
        extra_args=("-p", "reexport_home"),
        extra_env={"REAL_HOME": str(tmp_path / "real-home")},
        probe_source="def test_never_runs():\n    raise AssertionError('guard did not fire')\n",
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not isolated from the real home directory" in result.stdout + result.stderr
