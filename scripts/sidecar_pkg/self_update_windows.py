"""The detached Windows helper that swaps a running sidecar executable.

Split out of ``self_update.py``. ``_apply_windows`` stays in ``self_update``
(it releases the lock and runs the pre-exec hooks, both of which tests patch
on that module) and calls ``_windows_swap_script`` through it.
"""

from __future__ import annotations

import pathlib


def _windows_swap_script(
    pid: int,
    install: pathlib.Path,
    incoming: pathlib.Path,
    backup: pathlib.Path,
    *,
    restart: bool,
    display_version: str | None,
) -> str:
    """Batch helper: wait for *pid* and the onefile bootloader to exit, move *install* → *backup* and
    *incoming* → *install* (restoring on failure), refresh the installer's
    DisplayVersion, relaunch, and delete itself. Its log is kept beside the
    helper because the parent process exits before the swap finishes."""
    saved_backup = backup.with_name(backup.name + "_old")
    # Looked up on the apply module at call time. Importing it here would cycle
    # (self_update imports this helper), and a patch of these names on
    # self_update still applies. The apply module is already loaded: it is
    # the caller.
    import sys

    apply = sys.modules["scripts.sidecar_pkg.self_update"]

    version = apply._previous_version_path(install)
    saved_version = saved_backup.with_name(saved_backup.name + apply._VERSION_SUFFIX)
    pending_version = install.with_name(install.name + ".previous.pending.version")
    transaction = apply._windows_transaction_path(install)
    relaunch = f'start "" "{install}"' if restart else "rem no relaunch"
    reg = (
        f'reg query "{apply._WIN_UNINSTALL_KEY}" >NUL 2>&1 && '
        f'reg add "{apply._WIN_UNINSTALL_KEY}" /v DisplayVersion /t REG_SZ /d "{display_version}" /f >NUL\r\n'
        if display_version
        else ""
    )
    restart_log = "" if restart else 'echo [%date% %time%] Relaunch disabled.>>"%LOG%"\r\n'
    return (
        "@echo off\r\n"
        'set "LOG=%~dp0runway-self-update.log"\r\n'
        # A new onefile instance must unpack its own runtime. The helper
        # inherits the old frozen process's _PYI_* environment variables.
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"\r\n'
        'echo [%date% %time%] Self-update helper started.>"%LOG%"\r\n'
        f'echo [%date% %time%] Waiting for sidecar PID {pid} to exit.>>"%LOG%"\r\n'
        ":waitloop\r\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\r\n'
        "if not errorlevel 1 (\r\n"
        "  ping -n 2 127.0.0.1 >NUL\r\n"
        "  goto waitloop\r\n"
        ")\r\n"
        # Retain the pre-existing rollback slot until both incoming bytes and
        # their backup label are committed. Never overwrite interrupted state.
        'set "BACKUP_SAVED=0"\r\n'
        'set "VERSION_SAVED=0"\r\n'
        'set "ORIGINAL_MOVED=0"\r\n'
        'set "ORIGINAL_RESTORED=0"\r\n'
        f'if exist "{saved_backup}" goto backup_conflict\r\n'
        f'if exist "{saved_version}" goto backup_conflict\r\n'
        f'if not exist "{pending_version}" goto metadata_missing\r\n'
        f'if exist "{backup}" (\r\n'
        f'  move /Y "{backup}" "{saved_backup}" >>"%LOG%" 2>&1\r\n'
        "  if errorlevel 1 goto swap_failed\r\n"
        '  set "BACKUP_SAVED=1"\r\n'
        ")\r\n"
        f'if exist "{version}" (\r\n'
        f'  copy /B /Y "{version}" "{saved_version}" >>"%LOG%" 2>&1\r\n'
        "  if errorlevel 1 goto swap_failed\r\n"
        '  set "VERSION_SAVED=1"\r\n'
        ")\r\n"
        # PyInstaller's onefile parent remains alive briefly after its Python
        # child exits to clean up the extraction directory. Retry the rename
        # until Windows releases the image, with a bound so failures are logged.
        "set /a wait_attempts=0 >NUL\r\n"
        ":move_original\r\n"
        f'move /Y "{install}" "{backup}" >NUL 2>&1\r\n'
        "if not errorlevel 1 goto install_new\r\n"
        "set /a wait_attempts+=1 >NUL\r\n"
        "if %wait_attempts% GEQ 120 goto move_failed\r\n"
        "ping -n 2 127.0.0.1 >NUL\r\n"
        "goto move_original\r\n"
        ":install_new\r\n"
        'set "ORIGINAL_MOVED=1"\r\n'
        f'move /Y "{incoming}" "{install}" >>"%LOG%" 2>&1\r\n'
        "if errorlevel 1 goto restore_previous\r\n"
        f'move /Y "{pending_version}" "{version}" >>"%LOG%" 2>&1\r\n'
        "if errorlevel 1 goto restore_new\r\n"
        f'del "{saved_backup}" "{saved_version}" "{transaction}" >NUL 2>&1\r\n'
        'echo [%date% %time%] Installed updated sidecar.>>"%LOG%"\r\n'
        f"{reg}"
        f"{relaunch}\r\n"
        f"{restart_log}"
        'del "%~f0"\r\n'
        "exit /b 0\r\n"
        ":restore_new\r\n"
        'echo [%date% %time%] Backup version commit failed; undoing replacement.>>"%LOG%"\r\n'
        f'move /Y "{install}" "{incoming}" >>"%LOG%" 2>&1\r\n'
        "if errorlevel 1 goto restore_failed\r\n"
        ":restore_previous\r\n"
        'echo [%date% %time%] New executable move failed; restoring previous sidecar.>>"%LOG%"\r\n'
        f'if not exist "{backup}" goto restore_failed\r\n'
        f'move /Y "{backup}" "{install}" >>"%LOG%" 2>&1\r\n'
        "if errorlevel 1 goto restore_failed\r\n"
        'set "ORIGINAL_RESTORED=1"\r\n'
        "goto swap_failed\r\n"
        ":restore_failed\r\n"
        'echo [%date% %time%] Failed to restore previous sidecar from backup.>>"%LOG%"\r\n'
        "goto swap_failed\r\n"
        ":move_failed\r\n"
        f'echo [%date% %time%] Original sidecar PID {pid} exited, but the executable stayed locked after %wait_attempts% move attempts.>>"%LOG%"\r\n'
        'tasklist /FI "IMAGENAME eq RunwaySidecar.exe" >>"%LOG%" 2>&1\r\n'
        "goto swap_failed\r\n"
        ":backup_conflict\r\n"
        'echo [%date% %time%] Existing saved rollback state requires recovery; refusing overwrite.>>"%LOG%"\r\n'
        "goto swap_failed\r\n"
        ":metadata_missing\r\n"
        'echo [%date% %time%] Missing staged backup version; refusing swap.>>"%LOG%"\r\n'
        "goto swap_failed\r\n"
        ":swap_failed\r\n"
        'set "RESTORE_SAVED=0"\r\n'
        'if "%ORIGINAL_MOVED%"=="0" set "RESTORE_SAVED=1"\r\n'
        'if "%ORIGINAL_RESTORED%"=="1" set "RESTORE_SAVED=1"\r\n'
        'if "%RESTORE_SAVED%"=="1" (\r\n'
        '  if "%BACKUP_SAVED%"=="1" (\r\n'
        f'    move /Y "{saved_backup}" "{backup}" >>"%LOG%" 2>&1\r\n'
        "    if errorlevel 1 goto saved_restore_failed\r\n"
        "  )\r\n"
        '  if "%VERSION_SAVED%"=="1" (\r\n'
        f'    move /Y "{saved_version}" "{version}" >>"%LOG%" 2>&1\r\n'
        "    if errorlevel 1 goto saved_restore_failed\r\n"
        "  )\r\n"
        ")\r\n"
        'if "%RESTORE_SAVED%"=="0" goto failure_done\r\n'
        f'del "{pending_version}" "{transaction}" >NUL 2>&1\r\n'
        "goto failure_done\r\n"
        ":saved_restore_failed\r\n"
        'echo [%date% %time%] Saved rollback restoration failed; preserving recovery files.>>"%LOG%"\r\n'
        ":failure_done\r\n"
        'echo [%date% %time%] Sidecar swap failed; check file permissions and running RunwaySidecar processes.>>"%LOG%"\r\n'
        # The first move may have failed before the original was renamed;
        # relaunch it when it is still present.
        f'if exist "{install}" {relaunch}\r\n'
        'del "%~f0"\r\n'
        "exit /b 1\r\n"
    )
