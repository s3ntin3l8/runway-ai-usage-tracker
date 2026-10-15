"""Environment isolation for the throwaway demo instance (`make demo`, `make screenshots`).

The demo runs on hosts that may also run a real Runway (container or `make run`),
often from a developer shell full of secrets. The concrete hazards:

- pydantic-settings loads `.env` from the working directory (app/core/config.py),
  and a dev checkout's `.env` carries real provider keys, ADMIN/INGEST keys and a
  GITHUB_TOKEN; `make dev` additionally sources it into the shell.
- With RUNWAY_CONFIG_DIR unset the database defaults to ~/.config/runway, which is
  where a host deployment keeps its real data.
- Collectors look in HOME for CLI logins, keychains and browser cookies.

So every demo process gets an environment built from scratch (only PATH and
locale are inherited), HOME / RUNWAY_CONFIG_DIR / XDG dirs point into one scratch
directory, the server runs from a working directory without a `.env`, and
`assert_demo_env` refuses to start if anything still points elsewhere.
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Iterable, Mapping
from pathlib import Path

PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TERM", "TZ")

# Keys the demo sets itself with values it generates for this run.
# The demo sets no secrets at all (ADMIN_API_KEY would also require DB_ENCRYPTION_KEY).
GENERATED_SECRETS: frozenset[str] = frozenset()
SECRET_KEY = re.compile(r"(SECRET|PASSWORD|_KEY|_TOKEN)$|ENCRYPTION")
PATH_KEY = re.compile(r"(_DIR|_PATH|_HOME)$")
# Path-ish keys that legitimately point outside the scratch dir.
PATH_ALLOW = frozenset({"PLAYWRIGHT_BROWSERS_PATH", "SCREENSHOTS_OUT_DIR"})
MUST_BE_INSIDE = ("HOME", "TMPDIR", "RUNWAY_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
FORBIDDEN = frozenset({"TRUSTED_PROXY_IPS", "PUBLIC_URL"})
# The default port of a real local Runway; a demo must never take it.
RESERVED_PORTS = frozenset({"8765"})


def demo_env(
    demo_dir: Path, extra: Mapping[str, str] | None = None, parent: Mapping[str, str] | None = None
) -> dict[str, str]:
    """A clean environment for a demo child process."""
    parent = os.environ if parent is None else parent
    env = {k: parent[k] for k in PASSTHROUGH if k in parent}
    env.update(
        {
            "HOME": str(demo_dir / "home"),
            "TMPDIR": str(demo_dir / "tmp"),
            "XDG_CONFIG_HOME": str(demo_dir / "home" / ".config"),
            "XDG_DATA_HOME": str(demo_dir / "home" / ".local" / "share"),
            "RUNWAY_CONFIG_DIR": str(demo_dir / "config"),
            # No browser-cookie or keychain lookups in the demo.
            "BROWSER_PREFERENCE": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env.update(extra or {})
    return env


def _inside(base: Path, value: str) -> bool:
    p = Path(value)
    if not p.is_absolute():
        return False
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def demo_env_problems(env: Mapping[str, str], demo_dir: Path, cwd: Path | None = None) -> list[str]:
    """Everything that would let the demo reach outside `demo_dir`."""
    problems: list[str] = []
    for key, value in env.items():
        if key.startswith("MULLION_"):
            problems.append(f"{key} must not reach the demo")
        if key in FORBIDDEN:
            problems.append(f"{key} must not be set in the demo")
        if SECRET_KEY.search(key) and key not in GENERATED_SECRETS:
            problems.append(f"{key} looks like a real secret; the demo never sets it")
        if (
            PATH_KEY.search(key)
            and key not in PATH_ALLOW
            and os.path.isabs(value)
            and not _inside(demo_dir, value)
        ):
            problems.append(f"{key}={value} is outside the demo dir")
    for key in MUST_BE_INSIDE:
        if key not in env or not _inside(demo_dir, env[key]):
            problems.append(f"{key} must be set inside the demo dir")
    if "DATABASE_PATH" in env and not _inside(demo_dir, env["DATABASE_PATH"]):
        problems.append("DATABASE_PATH must be inside the demo dir")
    if env.get("APP_HOST", "127.0.0.1") != "127.0.0.1":
        problems.append("APP_HOST must be 127.0.0.1")
    if env.get("APP_PORT") in RESERVED_PORTS:
        problems.append(f"APP_PORT {env['APP_PORT']} is reserved for a real instance")
    if cwd is not None and (cwd / ".env").exists():
        problems.append(f"{cwd} contains a .env that the app would load")
    return problems


def assert_demo_env(env: Mapping[str, str], demo_dir: Path, cwd: Path | None = None) -> None:
    problems = demo_env_problems(env, demo_dir, cwd)
    if problems:
        raise RuntimeError("demo environment refused:\n  - " + "\n  - ".join(problems))


def assert_ports_free(ports: Iterable[int], hosts: Iterable[str] = ("127.0.0.1", "::1")) -> None:
    """Raise if anything already listens on one of the ports (likely a real instance)."""
    for port in ports:
        for host in hosts:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((host, int(port))) == 0:
                    raise RuntimeError(
                        f"port {port} is already in use on {host}; refusing to start the demo"
                    )
