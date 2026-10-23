"""Read-only probe for the MiniMax Code runtime store.

The MiniMax Code runtime keeps its own usage ledger on disk, and the
sidecar reads it to report the traffic as usage events. This probe answers
the question a user (or a reviewer on a new platform) actually has: *will
the sidecar on this host see my MiniMax Code usage, and what does it see?*

It is a diagnostic, not a collector:

- it opens the store **read-only**, exactly like the extractor;
- it runs the real extractor, so the numbers it prints are the numbers the
  sidecar would push;
- it **never commits the cursor** — the parked advance stays in memory and
  the cursor file on disk is left untouched, so running the probe cannot
  make the sidecar skip rows.

Every platform resolves the runtime's data dir from ``homedir()``, so the
same probe covers Linux, macOS and Windows.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# The columns the extractor needs per table. This mirrors the extractor's
# own requirement (``_REQUIRED_COLUMNS``); a unit test pins the two
# together so they cannot drift.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "local_runtime_token_usage": (
        "id",
        "session_id",
        "turn_id",
        "ts",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ),
    "local_runtime_message_rows": ("id", "session_id", "turn_id", "data_json"),
    "local_runtime_sessions": (
        "session_id",
        "workspace_dir",
        "extra_data_json",
        "history_relative_dir",
    ),
}


# How far back the probe looks when the host has no cursor yet — the same
# window the sidecar imports on its first run, read from one place.
from scripts.sidecar_pkg.bootstrap import bootstrap_window_days  # noqa: E402

# The extractor's per-cycle row cap, quoted in the report so a reader can
# tell "the ledger holds N requests" from "this cycle reads at most N".
_MAX_ROWS_PER_CYCLE = 5000


def probe_command(argv: list[str]) -> bool:
    """Handle ``--probe-minimax-code``; return False when the flag is absent.

    Exits with 0 when the store was found and the extractor accepted it,
    and with 1 when there is nothing to read (no store) or the store's
    schema has drifted away from what the extractor understands.
    """
    if not argv or argv[0] != "--probe-minimax-code":
        return False

    parser = argparse.ArgumentParser(
        prog="--probe-minimax-code",
        description="Report what the sidecar can read from the MiniMax Code runtime store",
    )
    parser.add_argument(
        "--probe-minimax-code",
        action="store_true",
        help="(the flag itself selects this diagnostic)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON instead of text",
    )
    args = parser.parse_args(argv)

    report = probe_minimax_code()
    if args.json:
        _emit(json.dumps(report, indent=2, default=str))
    else:
        _emit(format_report(report))
    raise SystemExit(0 if report["ok"] else 1)


def _store_path() -> Path | None:
    """Return the runtime store path, or None when the runtime never ran."""
    from scripts.sidecar_pkg.event_extractors.minimax_code import discover_runtime_store

    return discover_runtime_store()


def _probed_roots() -> list[str]:
    """Return the data roots the extractor probes, in order."""
    from scripts.sidecar_pkg.event_extractors.minimax_code import _data_roots

    return [str(root) for root in _data_roots()]


def _cursor_path() -> Path:
    """Return the cursor file the sidecar uses for this host."""
    from scripts.sidecar_pkg.event_extractors.minimax_code import _default_state_path

    return _default_state_path()


def _introspect(db_path: Path) -> dict[str, Any]:
    """Report the tables and columns the store actually has."""
    found: dict[str, list[str]] = {}
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception as exc:  # pragma: no cover - depends on the host's file state
        return {
            "error": f"cannot open the store read-only: {exc}",
            "tables": {},
            "missing_tables": list(REQUIRED_COLUMNS),
            "missing_columns": {},
        }
    try:
        cur = conn.cursor()
        for table, required in REQUIRED_COLUMNS.items():
            row = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None:
                missing_tables.append(table)
                continue
            columns = [info[1] for info in cur.execute(f"PRAGMA table_info({table})")]
            found[table] = columns
            absent = [column for column in required if column not in columns]
            if absent:
                missing_columns[table] = absent
    except sqlite3.Error as exc:
        return {
            "error": f"cannot read the store: {exc}",
            "tables": {},
            "missing_tables": list(REQUIRED_COLUMNS),
            "missing_columns": {},
        }
    finally:
        conn.close()
    return {
        "error": None,
        "tables": found,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
    }


def _ledger_counts(db_path: Path) -> dict[str, int]:
    """Count the ledger's rows, turns and sessions (read-only)."""
    counts = {"requests": 0, "turns": 0, "sessions": 0}
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except Exception:
        return counts
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT COUNT(*), COUNT(DISTINCT turn_id), COUNT(DISTINCT session_id) "
            "FROM local_runtime_token_usage"
        ).fetchone()
        if row:
            counts = {
                "requests": int(row[0] or 0),
                "turns": int(row[1] or 0),
                "sessions": int(row[2] or 0),
            }
    except sqlite3.Error:
        # An unreadable ledger still leaves a usable report: the schema was
        # checked above, so the counts are the only thing missing.
        pass
    finally:
        conn.close()
    return counts


def _attach_parent_console() -> int | None:
    """Attach to the console the user launched from.

    PyInstaller's ``console=False`` build has ``sys.stdout is None``, so a
    command that prints raises ``AttributeError`` instead of printing.
    Re-attaching to the parent console is the standard fix; it fails
    harmlessly when there is no console to attach to.

    Returns the console's previous output code page so the caller can put
    it back: the code page is process-wide, and leaving the user's cmd
    window on UTF-8 changes what every later command prints.
    """
    if sys.platform != "win32" or sys.stdout is not None:
        return None
    previous_code_page: int | None = None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return None
        previous_code_page = int(kernel32.GetConsoleOutputCP())
        # CONOUT$ is bytes decoded with the console's active code page, so
        # the report's em dash and middle dot would land as mojibake on a
        # cp850 console. UTF-8 is what the report is written in.
        kernel32.SetConsoleOutputCP(65001)
        try:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        except Exception:
            # The code page is already switched: put it back before giving
            # up, or the user's console stays on UTF-8 for everything that
            # follows.
            _restore_console_code_page(previous_code_page)
            raise
        return previous_code_page
    except Exception:
        # A console that cannot be attached to is not a reason to fail the
        # report: the caller falls back to writing it to a file.
        return None


def _restore_console_code_page(previous: int | None) -> None:
    """Put the console's output code page back the way the user had it."""
    if previous is None:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(previous)
    except Exception:
        # Nothing to restore to; the console keeps whatever it had.
        pass


def _write_report_file(text: str) -> Path | None:
    """Write the report next to the sidecar's config, for a build with no console.

    Returns the path it wrote, or None when the write failed — a caller
    must not promise a report that does not exist.
    """
    path = _cursor_path().parent / "minimax_code_probe.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        # A read-only or missing config dir leaves nothing to point at.
        return None
    return path


def _notify_windows(message: str) -> None:
    """Show a message box; the only way a windowed build can talk to its user."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Runway Sidecar", 0x40)
    except Exception:
        # No message box available: the report file is the fallback.
        pass


def _emit(text: str) -> None:
    """Print the report — or put it where a windowed build's user can find it."""
    previous_code_page: int | None = None
    if sys.stdout is None:
        previous_code_page = _attach_parent_console()
    if sys.stdout is not None:
        # The report carries an em dash and a middle dot; a console that is
        # not UTF-8 would raise UnicodeEncodeError on them.
        from app.core.console import force_utf8_console

        force_utf8_console()
        try:
            print(text)
        finally:
            # Flush before restoring: a buffered tail must not be decoded
            # with the old code page.
            try:
                sys.stdout.flush()
            except Exception:
                pass
            # The code page is process-wide: the user's console must not
            # stay on UTF-8 for whatever runs next.
            _restore_console_code_page(previous_code_page)
        return
    path = _write_report_file(text)
    if path is None:
        _notify_windows("The MiniMax Code probe could not write its report file.")
        return
    _notify_windows(f"The MiniMax Code probe wrote its report to:\n{path}")


def probe_minimax_code() -> dict[str, Any]:
    """Collect the report the probe prints. Read-only; nothing is written."""
    from scripts.sidecar_pkg.event_extractors.minimax_code import parse_minimax_code_events

    report: dict[str, Any] = {
        "platform": platform.system(),
        "ok": False,
        "roots": _probed_roots(),
        "store": None,
        "store_size_bytes": None,
        "store_modified": None,
        "schema_ok": False,
        "tables": {},
        "missing_tables": [],
        "missing_columns": {},
        "requests": 0,
        "turns": 0,
        "sessions": 0,
        "models": {},
        "plan_review_requests": 0,
        "cursor": None,
        "cursor_last_id": None,
        "pending_requests": 0,
        "problem": None,
    }

    db_path = _store_path()
    if db_path is None:
        report["problem"] = (
            "no MiniMax Code runtime store found — MiniMax Code has not run on this host "
            "(or it keeps its data somewhere else); the sidecar has nothing to report"
        )
        return report

    report["store"] = str(db_path)
    try:
        stat = db_path.stat()
        report["store_size_bytes"] = stat.st_size
        report["store_modified"] = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    except OSError:
        # A stat failure (the file vanished between the discovery and the
        # read) only costs the size and the timestamp in the report.
        pass

    schema = _introspect(db_path)
    report["tables"] = schema["tables"]
    report["missing_tables"] = schema["missing_tables"]
    report["missing_columns"] = schema["missing_columns"]
    report["schema_ok"] = not (
        schema["error"] or schema["missing_tables"] or schema["missing_columns"]
    )
    if schema["error"]:
        report["problem"] = schema["error"]
        return report
    if not report["schema_ok"]:
        report["problem"] = (
            "the runtime store's schema has drifted away from what the extractor needs"
        )
        return report

    counts = _ledger_counts(db_path)
    report["requests"] = counts["requests"]
    report["turns"] = counts["turns"]
    report["sessions"] = counts["sessions"]

    # The real extractor, on the real cursor — but its parked advance is
    # never committed, so the cursor file on disk stays exactly as it was.
    events = parse_minimax_code_events(
        db_path,
        account_id="probe",
        since=datetime.now(UTC) - timedelta(days=bootstrap_window_days()),
        state_file=_cursor_path(),
    )
    models: dict[str, int] = {}
    plan_review = 0
    for event in events:
        label = event.model_id or "(unresolved)"
        models[label] = models.get(label, 0) + 1
        if event.subagent_type == "Plan":
            plan_review += 1
    report["models"] = models
    report["plan_review_requests"] = plan_review
    report["pending_requests"] = len(events)

    cursor = _cursor_path()
    report["cursor"] = str(cursor)
    report["cursor_last_id"] = _cursor_last_id(cursor)

    report["ok"] = True
    report["problem"] = None
    return report


def _cursor_last_id(cursor: Path) -> int | None:
    """Read the cursor's last id without touching the file."""
    try:
        data = json.loads(cursor.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    databases = data.get("databases")
    if not isinstance(databases, dict):
        return None
    last: int | None = None
    for entry in databases.values():
        if isinstance(entry, dict):
            value = entry.get("last_id")
            if isinstance(value, int):
                last = value if last is None else max(last, value)
    return last


def format_report(report: dict[str, Any]) -> str:
    """Render the report as the text the probe prints."""
    lines = ["MiniMax Code runtime store — read-only probe", ""]
    lines.append(f"  platform            : {report['platform']}")
    lines.append(f"  probed roots        : {', '.join(report['roots'])}")
    if not report["store"]:
        lines.append("  store               : (not found)")
    else:
        size = report["store_size_bytes"]
        size_text = f"{size / 1024 / 1024:.1f} MiB" if isinstance(size, int) else "unknown"
        lines.append(f"  store               : {report['store']} ({size_text})")
        if report["store_modified"]:
            lines.append(f"  store modified      : {report['store_modified']}")
    if report["missing_tables"]:
        lines.append(f"  missing tables      : {', '.join(report['missing_tables'])}")
    for table, columns in report["missing_columns"].items():
        lines.append(f"  missing columns     : {table} → {', '.join(columns)}")
    lines.append(f"  schema              : {'OK' if report['schema_ok'] else 'INCOMPATIBLE'}")
    if report["store"]:
        lines.append(
            f"  ledger              : {report['requests']} requests"
            f" · {report['turns']} turns · {report['sessions']} sessions"
        )
        lines.append(f"  plan-review rows    : {report['plan_review_requests']}")
        for model, count in sorted(report["models"].items()):
            lines.append(f"  model {model:<27}: {count}")
        lines.append(f"  cursor              : {report['cursor']}")
        last = report["cursor_last_id"]
        lines.append(f"  cursor last id      : {last if last is not None else '(none)'}")
        lines.append(
            f"  this cycle would push: {report['pending_requests']} request(s)"
            f" (a cycle reads at most {_MAX_ROWS_PER_CYCLE} rows)"
        )
    lines.append("")
    if report["ok"]:
        lines.append(
            "  RESULT: PASS — the sidecar on this host reports MiniMax Code usage "
            "as provider 'minimax' / app 'minimax_code'."
        )
    else:
        lines.append(f"  RESULT: FAIL — {report['problem']}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    probe_command(sys.argv[1:])
