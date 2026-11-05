"""Decide how a POSIX self-update relaunches: systemd restart, or exec.

Split out of ``self_update.py``. ``_relaunch_posix`` stays in ``self_update``
because it calls ``_release_lock`` and the pre-exec hooks, which tests replace
on that module. It calls ``_systemd_restart_exit_code`` through ``self_update``,
so a patch of that name still wins.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys

from scripts.sidecar_pkg import runtime_cleanup
from scripts.sidecar_pkg.self_update_fetch import _patched

logger = logging.getLogger("scripts.sidecar_pkg.self_update")


def _fresh_runtime_env(*, hand_off_runtime: bool = False) -> dict[str, str]:
    """Environment for a relaunched onefile build.

    The running PyInstaller child carries ``_PYI_*`` variables pointing at its own
    extracted ``_MEI`` dir. Inherited by the new binary, they make its bootloader
    run the OLD unpacked code, so the update installs but never takes effect.
    ``PYINSTALLER_RESET_ENVIRONMENT`` forces a fresh unpack (mirrors the Windows
    helper script); the ``_PYI_*`` keys are dropped as well for good measure.

    With *hand_off_runtime* (an in-place ``execve``) the old ``_MEI`` dir is named in
    ``RUNWAY_RETIRED_MEIPASS``: the old parent bootloader keeps waiting on the re-exec'd
    image, so nothing else would ever delete it.
    """
    # A copy, so os.environ is untouched if execve fails and this image keeps running.
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if hand_off_runtime:
        runtime = runtime_cleanup.own_runtime_dir()
        if runtime is not None:
            env[runtime_cleanup.RETIRED_ENV] = str(runtime)
    return env


# systemd exit codes that make ``Restart=`` respawn the unit (``always`` restarts on any exit).
_EX_TEMPFAIL = 75
_SYSTEMCTL_TIMEOUT_S = 5


def _systemd_unit_from_cgroup(cgroup: str) -> tuple[str, bool] | None:
    """``(unit name, is_user_manager)`` for the service a ``/proc/<pid>/cgroup`` dump is in."""
    parts = [p for line in cgroup.splitlines() for p in line.rpartition(":")[2].split("/")]
    services = [p for p in parts if p.endswith(".service")]
    if not services:
        return None
    user_manager = any(p.startswith("user@") and p.endswith(".service") for p in parts)
    return services[-1], user_manager


# Far above any realistic nesting (one level per update). Its real job is to bound a cyclic or
# runaway ppid walk, so don't raise it to "support" deeper chains.
_MAX_BOOTLOADER_DEPTH = 64


def _bootloader_chain_reaches(main_pid: int) -> bool:
    """True if ``main_pid`` is our parent, or an ancestor reached only through our own copies.

    An in-place ``execve`` update leaves the old onefile bootloader waiting on the new one, so a
    host that updated that way runs ``MainPID -> stale bootloader -> ... -> python``. Every stale
    level was re-exec'd with our exact argv, so those levels share ``/proc/self/cmdline``. Walking
    through anything else (a shell, a wrapper) could mean exiting does not end the unit, so that
    stops the walk. Any doubt returns False.
    """
    if main_pid <= 1:
        return False
    try:
        own_cmdline = pathlib.Path("/proc/self/cmdline").read_bytes()
        pid = os.getppid()
        for _ in range(_MAX_BOOTLOADER_DEPTH):
            if pid == main_pid:
                return True
            if pid <= 1 or pathlib.Path(f"/proc/{pid}/cmdline").read_bytes() != own_cmdline:
                return False
            # ``/proc/<pid>/stat`` is ``pid (comm) state ppid ...``; comm may hold spaces/parens.
            pid = int(pathlib.Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return False
    return False


def _systemd_restart_exit_code() -> int | None:
    """Exit code that makes systemd restart us, or None if it is not our direct supervisor.

    ``INVOCATION_ID`` alone proves nothing: every process inside any systemd service has it,
    including one started from a shell in an unrelated service. The unit must have our onefile
    parent bootloader as its MainPID (or an ancestor of it that only stale copies of that
    bootloader separate us from, see ``_bootloader_chain_reaches``), so that this process exiting
    really ends the unit, and a ``Restart=`` policy that respawns it. Any doubt returns None and
    the caller falls back to ``execve``.
    """
    if sys.platform != "linux" or not os.environ.get("INVOCATION_ID"):
        return None
    try:
        found = _systemd_unit_from_cgroup(pathlib.Path("/proc/self/cgroup").read_text())
        if found is None:
            return None
        unit, user_manager = found
        proc = subprocess.run(  # noqa: S603
            [
                "systemctl",
                *(["--user"] if user_manager else []),
                "show",
                "-p",
                "MainPID",
                "-p",
                "Restart",
                unit,
            ],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_patched("_SYSTEMCTL_TIMEOUT_S"),
            check=False,
        )
        props = dict(line.partition("=")[::2] for line in proc.stdout.splitlines())
        if proc.returncode != 0 or not _bootloader_chain_reaches(int(props.get("MainPID", "0"))):
            return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return {"always": 0, "on-failure": _EX_TEMPFAIL}.get(props.get("Restart", ""))
