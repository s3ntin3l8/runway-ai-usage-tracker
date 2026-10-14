"""Registry login retries preserve fatal failures and keep credentials on stdin."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("failures,attempts,code", [(0, 1, 0), (2, 3, 0), (5, 4, 1)])
def test_registry_login_bounded_retries(tmp_path, failures, attempts, code):
    fake_docker = tmp_path / "fake_docker.py"
    fake_docker.write_text("""
import os,sys
from pathlib import Path
count=Path(os.environ['ATTEMPTS_FILE'])
n=int(count.read_text(encoding="utf-8"))+1 if count.exists() else 1
count.write_text(str(n))
assert sys.argv[1:]==['login','registry.example','-u','test-user','--password-stdin']
assert sys.stdin.read()==os.environ['REGISTRY_PASS']
sys.exit(1 if n<=int(os.environ['FAILURES']) else 0)
""")
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\nexec "
        + shlex.quote(Path(sys.executable).as_posix())
        + " "
        + shlex.quote(fake_docker.as_posix())
        + ' "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    docker.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "$SLEEPS_FILE"\n',
        encoding="utf-8",
        newline="\n",
    )
    sleep.chmod(0o755)
    env = {
        **os.environ,
        "TEST_BIN": str(tmp_path),
        "REGISTRY_HOST": "registry.example",
        "REGISTRY_USER": "test-user",
        "REGISTRY_PASS": "synthetic-sensitive-token",
        "FAILURES": str(failures),
        "ATTEMPTS_FILE": str(tmp_path / "attempts"),
        "SLEEPS_FILE": (tmp_path / "sleeps").as_posix(),
    }
    script = Path(__file__).resolve().parents[2] / ".github/actions/registry-login/login.sh"
    action = tmp_path / "login.sh"
    action.write_text(script.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    bash = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        # Git hooks put mingw64/bin ahead of cmd on PATH. Find the same Git
        # installation's Bash in either layout instead of declaring it absent.
        candidates = (
            (parent / "bin" / "bash.exe" for parent in Path(git).resolve().parents) if git else ()
        )
        candidate = next((path for path in candidates if path.is_file()), None)
        if candidate is not None:
            bash = str(candidate)
        else:
            pytest.skip("Git Bash is required to test the Linux registry-login action on Windows")
    if bash is None:
        pytest.skip("Bash is required to test the registry-login action")
    env["TEST_SCRIPT"] = str(action)
    bin_path = '$(cygpath -u "$TEST_BIN")' if os.name == "nt" else "$TEST_BIN"
    command = f'export PATH="{bin_path}:$PATH"; exec bash "$TEST_SCRIPT"'
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == code
    assert (tmp_path / "attempts").read_text(encoding="utf-8") == str(attempts)
    assert "synthetic-sensitive-token" not in result.stdout + result.stderr
    expected = [str(n * 10) for n in range(1, attempts)]
    assert (
        (tmp_path / "sleeps").read_text(encoding="utf-8").splitlines() == expected
        if expected
        else not (tmp_path / "sleeps").exists()
    )
