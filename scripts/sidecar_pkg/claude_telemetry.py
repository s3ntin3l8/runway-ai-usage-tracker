"""Manage Claude Code's ~/.claude/settings.json OpenTelemetry env block (#1366).

When the operator opts in, the sidecar ensures the OTLP/HTTP-JSON env vars
pointing at ``127.0.0.1:4318`` are present in ``~/.claude/settings.json``.
When turned off, it removes only the keys it added (recorded in its own
state file) and removes ``env`` only if it created it and it is now empty.

Safety invariants:
- Never modifies any other key or reorders existing keys.
- Preserves the file's existing indentation style.
- Writes atomically (temp file in same directory + fsync + os.replace) and
  aborts if the file was modified concurrently between read and write.
- Writes a one-time backup (``settings.json.runway.bak``) before the first
  change to an existing file.
- Never overwrites a key the user already set to a different value (reports
  ``conflict``) or an unparseable/malformed file (reports ``error``).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from scripts.sidecar_pkg.local_paths import get_sidecar_dir
from scripts.sidecar_pkg.otel_receiver import BIND_HOST, DEFAULT_PORT

TELEMETRY_ENV_KEYS: dict[str, str] = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{BIND_HOST}:{DEFAULT_PORT}",
    "OTEL_LOGS_EXPORT_INTERVAL": "5000",
}

VALID_STATUSES = ("disabled", "enabled", "conflict", "error")


def default_settings_path() -> Path:
    """Return ``~/.claude/settings.json`` (same path on Linux, macOS, and Windows)."""
    return Path.home() / ".claude" / "settings.json"


def default_state_path() -> Path:
    """Return the sidecar's state file recording keys added to Claude settings."""
    return get_sidecar_dir() / "claude-telemetry-state.json"


def backup_path_for(settings_path: Path) -> Path:
    """Return the one-time backup path next to *settings_path*."""
    return settings_path.with_name(f"{settings_path.name}.runway.bak")


def _detect_indent(text: str) -> int | str:
    """Detect the existing JSON indentation (spaces count or ``'\\t'``); default 2."""
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped in {"{", "}", "[", "]"}:
            continue
        leading = line[: len(line) - len(line.lstrip(" \t"))]
        if not leading:
            return 0
        if leading[0] == "\t":
            return "\t"
        return len(leading)
    return 2


def _detect_format(raw_text: str) -> tuple[int | str | None, str, str]:
    """Return ``(indent, newline, trailing_nl)`` matching *raw_text*'s formatting."""
    newline = "\r\n" if "\r\n" in raw_text else "\n"
    trailing_nl = raw_text[len(raw_text.rstrip("\r\n")) :]
    if "\n" not in raw_text.rstrip("\r\n"):
        return None, newline, trailing_nl
    return _detect_indent(raw_text), newline, trailing_nl


def _format_settings_json(
    data: dict[str, Any],
    indent: int | str | None,
    newline: str,
    trailing_nl: str,
) -> bytes:
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    if newline != "\n":
        text = text.replace("\n", newline)
    return (text + trailing_nl).encode("utf-8")


def _stat_sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def settings_stat_sig(path: Path) -> tuple[int, int] | None:
    """Best-effort ``(mtime_ns, size)`` signature for change detection; None on OSError."""
    try:
        return _stat_sig(path)
    except OSError:
        return None


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    check_stat: bool = False,
    expected_stat: tuple[int, int] | None = None,
) -> bool:
    """Write *content* to *path* atomically (resolving symlinks). Returns False on concurrent edit."""
    write_target = Path(os.path.realpath(path))
    write_target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(write_target.parent),
        prefix=f".{write_target.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if check_stat and _stat_sig(path) != expected_stat:
            tmp_path.unlink(missing_ok=True)
            return False
        os.replace(tmp_path, write_target)
        return True
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_state(state_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"added_keys": [], "created_env": False}
    if not isinstance(raw, dict):
        return {"added_keys": [], "created_env": False}
    added = [k for k in raw.get("added_keys", []) if isinstance(k, str)]
    return {"added_keys": added, "created_env": bool(raw.get("created_env", False))}


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    _atomic_write_bytes(
        state_path,
        (json.dumps(state, indent=2) + "\n").encode("utf-8"),
    )


def _warn(warn: bool, msg: str, *args: Any) -> None:
    if warn:
        logging.warning(msg, *args)


def _read_settings(
    settings_path: Path,
    *,
    warn: bool = True,
) -> tuple[dict[str, Any], str, bytes, tuple[int, int]] | None:
    """Read and parse *settings_path*, returning None if unparseable or malformed."""
    try:
        expected_stat = _stat_sig(settings_path)
        if expected_stat is None:
            return None
        raw_bytes = settings_path.read_bytes()
        raw_text = raw_bytes.decode("utf-8")
        data = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _warn(warn, "Cannot parse %s (%s); leaving file untouched", settings_path, exc)
        return None
    if not isinstance(data, dict):
        _warn(warn, "Expected JSON object in %s; leaving file untouched", settings_path)
        return None
    if "env" in data and not isinstance(data["env"], dict):
        _warn(
            warn,
            "Expected 'env' to be an object in %s; leaving file untouched",
            settings_path,
        )
        return None
    return data, raw_text, raw_bytes, expected_stat


def sync_claude_telemetry(
    enabled: bool,
    *,
    settings_path: Path | None = None,
    state_path: Path | None = None,
    warn: bool = True,
) -> str:
    """Apply or remove Claude Code telemetry env vars in ``settings.json``.

    Returns one of ``"disabled"``, ``"enabled"``, ``"conflict"``, or ``"error"``.
    """
    target = settings_path or default_settings_path()
    state_file = state_path or default_state_path()

    try:
        if not enabled:
            return _disable(target, state_file, warn=warn)
        return _enable(target, state_file, warn=warn)
    except OSError as exc:
        _warn(warn, "Failed to update %s (%s); leaving file untouched", target, exc)
        return "error"


def _enable(settings_path: Path, state_path: Path, *, warn: bool = True) -> str:
    state = _load_state(state_path)
    file_exists = settings_path.exists()
    if file_exists:
        parsed = _read_settings(settings_path, warn=warn)
        if parsed is None:
            return "error"
        data, raw_text, raw_bytes, expected_stat = parsed
        indent, newline, trailing_nl = _detect_format(raw_text)
    else:
        data = {}
        expected_stat = None
        indent, newline, trailing_nl = 2, "\n", "\n"

    env = data.get("env")
    if isinstance(env, dict):
        conflicts = [k for k, v in TELEMETRY_ENV_KEYS.items() if k in env and env[k] != v]
        if conflicts:
            _warn(
                warn,
                "Claude Code telemetry conflict in %s (%s already set to another value); "
                "leaving file untouched",
                settings_path,
                ", ".join(conflicts),
            )
            return "conflict"

    created_env_now = "env" not in data
    env_dict: dict[str, Any] = data.setdefault("env", {})
    newly_added = [k for k in TELEMETRY_ENV_KEYS if k not in env_dict]

    if file_exists and not newly_added and not created_env_now:
        if not state.get("added_keys") and not state.get("created_env"):
            _save_state(
                state_path,
                {
                    "added_keys": list(TELEMETRY_ENV_KEYS),
                    "created_env": set(env_dict.keys()) <= set(TELEMETRY_ENV_KEYS),
                },
            )
        return "enabled"

    if file_exists:
        bak = backup_path_for(settings_path)
        if not bak.exists():
            _atomic_write_bytes(bak, raw_bytes)

    for key in newly_added:
        env_dict[key] = TELEMETRY_ENV_KEYS[key]

    new_bytes = _format_settings_json(data, indent, newline, trailing_nl)
    if not _atomic_write_bytes(
        settings_path,
        new_bytes,
        check_stat=True,
        expected_stat=expected_stat,
    ):
        _warn(warn, "Concurrent edit detected on %s; leaving file untouched", settings_path)
        return "error"

    merged_added = list(dict.fromkeys([*state.get("added_keys", []), *newly_added]))
    merged_created_env = bool(state.get("created_env")) or created_env_now
    _save_state(state_path, {"added_keys": merged_added, "created_env": merged_created_env})
    return "enabled"


def _disable(settings_path: Path, state_path: Path, *, warn: bool = True) -> str:
    state = _load_state(state_path)
    added_keys: list[str] = state.get("added_keys", [])
    created_env: bool = bool(state.get("created_env", False))

    if not added_keys and not created_env:
        return "disabled"

    if not settings_path.exists():
        state_path.unlink(missing_ok=True)
        return "disabled"

    parsed = _read_settings(settings_path, warn=warn)
    if parsed is None:
        return "error"
    data, raw_text, _raw_bytes, expected_stat = parsed

    changed = False
    env = data.get("env")
    if isinstance(env, dict):
        for key in added_keys:
            if key in env:
                del env[key]
                changed = True
        if created_env and len(env) == 0:
            del data["env"]
            changed = True

    if changed:
        indent, newline, trailing_nl = _detect_format(raw_text)
        new_bytes = _format_settings_json(data, indent, newline, trailing_nl)
        if not _atomic_write_bytes(
            settings_path,
            new_bytes,
            check_stat=True,
            expected_stat=expected_stat,
        ):
            _warn(warn, "Concurrent edit detected on %s; leaving file untouched", settings_path)
            return "error"

    state_path.unlink(missing_ok=True)
    return "disabled"
