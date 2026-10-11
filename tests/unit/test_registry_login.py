"""Registry login retries preserve fatal failures and keep credentials on stdin."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("failures,attempts,code", [(0, 1, 0), (2, 3, 0), (5, 4, 1)])
def test_registry_login_bounded_retries(tmp_path, failures, attempts, code):
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import os,sys
from pathlib import Path
count=Path(os.environ['ATTEMPTS_FILE'])
n=int(count.read_text())+1 if count.exists() else 1
count.write_text(str(n))
assert sys.argv[1:]==['login','registry.example','-u','test-user','--password-stdin']
assert sys.stdin.read()==os.environ['REGISTRY_PASS']
sys.exit(1 if n<=int(os.environ['FAILURES']) else 0)
""")
    docker.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "$SLEEPS_FILE"\n')
    sleep.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "REGISTRY_HOST": "registry.example",
        "REGISTRY_USER": "test-user",
        "REGISTRY_PASS": "synthetic-sensitive-token",
        "FAILURES": str(failures),
        "ATTEMPTS_FILE": str(tmp_path / "attempts"),
        "SLEEPS_FILE": str(tmp_path / "sleeps"),
    }
    script = Path(__file__).resolve().parents[2] / ".github/actions/registry-login/login.sh"
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == code
    assert (tmp_path / "attempts").read_text() == str(attempts)
    assert "synthetic-sensitive-token" not in result.stdout + result.stderr
    expected = [str(n * 10) for n in range(1, attempts)]
    assert (
        (tmp_path / "sleeps").read_text().splitlines() == expected
        if expected
        else not (tmp_path / "sleeps").exists()
    )
