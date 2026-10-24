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

    def test_probe():
        root = Path(os.environ["TEST_ISOLATION_ROOT"]).resolve()
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


def test_a_bare_pytest_run_is_isolated_and_cleans_up(tmp_path):
    """No ``TEST_ISOLATION_ROOT`` from a runner: conftest makes and removes one."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "TEST_ISOLATION_ROOT"}
    env["HOME"] = env["USERPROFILE"] = str(real_home)
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp_path)
    env["PROBE_OUT"] = str(tmp_path / "root.txt")
    env.pop("RUNWAY_CONFIG_DIR", None)
    env.pop("PYTEST_XDIST_WORKER", None)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
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
    assert result.returncode == 0, result.stdout + result.stderr
    root = Path((tmp_path / "root.txt").read_text(encoding="utf-8"))
    assert root.name.startswith("runway-pytest-")
    assert root.parent in (tmp_path, tmp_path.resolve())
    # The temp root is removed at exit, and the "real" home was never written.
    assert not root.exists()
    assert list(real_home.iterdir()) == []
