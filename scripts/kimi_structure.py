"""Describe the structure of a Kimi Code data directory, never its content.

Run ``python scripts/kimi_structure.py <kimi-data-dir>`` and it prints one JSON
document with field names, value types, string lengths, boolean values and
counts -- so someone with Kimi Code installed can show what the data holds
(for instance whether a session title exists) without reading private files or
pasting a title. Pure standard library, read-only, no network.

Safety contract: no string value, session id, working directory, path or user
text is ever printed. Key names are field names; one that is not a plain
identifier is counted under ``other_keys`` instead of being echoed. The
argument is the only place a path is accepted and it is not echoed.
Symlinked files and directories are skipped (counted as unreadable), never followed.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

MAX_SESSIONS = 200
MAX_LINES_PER_FILE = 50_000
MAX_LINE_BYTES = 1024 * 1024
MAX_NEST_DEPTH = 6

_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_TITLE_NAME_HINTS = ("title", "name")
_NUMERIC_RECORD_TYPE = "token_counting.turn_recorded"


def value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "other"


def _safe_name(name: str) -> str | None:
    return name if _SAFE_NAME.match(name) else None


def _is_title_like(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _TITLE_NAME_HINTS)


class KeyStats:
    """Per-key counters: how many objects carry it, with which types."""

    def __init__(self) -> None:
        self.objects = 0
        self.types: dict[str, int] = {}
        self.min_len: int | None = None
        self.max_len: int | None = None
        self.true_count = 0
        self.false_count = 0

    def add(self, value: Any) -> None:
        self.objects += 1
        kind = value_type(value)
        self.types[kind] = self.types.get(kind, 0) + 1
        if isinstance(value, str):
            n = len(value)
            self.min_len = n if self.min_len is None else min(self.min_len, n)
            self.max_len = n if self.max_len is None else max(self.max_len, n)
        elif isinstance(value, bool):
            if value:
                self.true_count += 1
            else:
                self.false_count += 1

    def render(self, count_label: str) -> dict[str, Any]:
        out: dict[str, Any] = {count_label: self.objects, "types": dict(sorted(self.types.items()))}
        if self.min_len is not None:
            out["string_length"] = {"min": self.min_len, "max": self.max_len}
        if "boolean" in self.types:
            out["boolean_values"] = {"true": self.true_count, "false": self.false_count}
        return out


class KeySet:
    """Key statistics for one kind of object, plus a count of unprintable names."""

    def __init__(self) -> None:
        self.keys: dict[str, KeyStats] = {}
        self.other_keys = 0

    def observe(self, obj: dict[str, Any]) -> None:
        for key, value in obj.items():
            name = _safe_name(key) if isinstance(key, str) else None
            if name is None:
                self.other_keys += 1
                continue
            self.keys.setdefault(name, KeyStats()).add(value)

    def render(self, count_label: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "keys": {k: s.render(count_label) for k, s in sorted(self.keys.items())}
        }
        if self.other_keys:
            out["other_keys"] = self.other_keys
        return out


class TitleKeys:
    """Where title/name-like keys occur: (file kind, record type) -> key -> stats."""

    def __init__(self) -> None:
        self.found: dict[tuple[str, str, str], KeyStats] = {}
        self.unprintable: dict[tuple[str, str], int] = {}

    def observe(self, file_kind: str, record_type: str, obj: Any, depth: int = 0) -> None:
        if not isinstance(obj, dict) or depth > MAX_NEST_DEPTH:
            return
        for key, value in obj.items():
            if isinstance(key, str) and _is_title_like(key):
                name = _safe_name(key)
                if name is not None:
                    self.found.setdefault((file_kind, record_type, name), KeyStats()).add(value)
                else:  # counted, never echoed
                    loc = (file_kind, record_type)
                    self.unprintable[loc] = self.unprintable.get(loc, 0) + 1
            if isinstance(value, dict):
                self.observe(file_kind, record_type, value, depth + 1)
            elif isinstance(value, list):
                for item in value[:50]:
                    self.observe(file_kind, record_type, item, depth + 1)

    def render(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (file_kind, record_type, key), stats in sorted(self.found.items()):
            row = {"file": file_kind, "record_type": record_type, "key": key}
            row.update(stats.render("occurrences"))
            rows.append(row)
        for (file_kind, record_type), n in sorted(self.unprintable.items()):
            rows.append({"file": file_kind, "record_type": record_type, "other_title_keys": n})
        return rows


def _open_regular(path: Path) -> Any:
    """Open a regular file for reading without following a symlink.

    Raises OSError for a missing file, a symlink or anything not a regular file,
    so a link can never make the reader describe data outside the given directory.
    """
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise OSError("not a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return os.fdopen(fd, "rb")


def _read_lines(path: Path, stats: JsonlStats) -> Iterator[bytes | None]:
    """Yield each line's bytes, or None for an oversized line. Stops at the line cap.

    Counts a truncated file only when data remains past the cap.
    Raises OSError when the file cannot be opened.
    """
    with _open_regular(path) as handle:
        for _ in range(MAX_LINES_PER_FILE):
            chunk = handle.readline(MAX_LINE_BYTES + 1)
            if not chunk:
                return
            if len(chunk) > MAX_LINE_BYTES and not chunk.endswith(b"\n"):
                while True:  # drain the rest of the oversized line
                    rest = handle.readline(MAX_LINE_BYTES)
                    if not rest or rest.endswith(b"\n"):
                        break
                yield None
            else:
                yield chunk
        if handle.read(1):
            stats.files_truncated += 1


class JsonlStats:
    def __init__(self) -> None:
        self.files_read = 0
        self.files_unreadable = 0
        self.files_truncated = 0
        self.lines = 0
        self.invalid_lines = 0
        self.oversized_lines = 0
        self.non_object_lines = 0


def _scan_jsonl(path: Path, stats: JsonlStats) -> Iterator[dict[str, Any]]:
    try:
        for raw in _read_lines(path, stats):
            stats.lines += 1
            if raw is None:
                stats.oversized_lines += 1
                continue
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (ValueError, RecursionError):
                stats.invalid_lines += 1
                continue
            if not isinstance(record, dict):
                stats.non_object_lines += 1
                continue
            yield record
        stats.files_read += 1
    except OSError:
        stats.files_unreadable += 1


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _sorted_children(path: Path, prefix: str) -> list[Path]:
    try:
        names = sorted(n for n in os.listdir(path) if n.startswith(prefix))
    except OSError:
        return []
    return [path / n for n in names if _is_real_dir(path / n)]


def _render_jsonl_counters(stats: JsonlStats) -> dict[str, int]:
    return {
        "files_read": stats.files_read,
        "files_missing_or_unreadable": stats.files_unreadable,
        "files_truncated_at_line_cap": stats.files_truncated,
        "lines": stats.lines,
        "invalid_lines": stats.invalid_lines,
        "non_object_lines": stats.non_object_lines,
        "oversized_lines": stats.oversized_lines,
    }


def describe(root: Path) -> dict[str, Any]:
    titles = TitleKeys()

    session_dirs: list[Path] = []
    workdirs = _sorted_children(root / "sessions", "wd_")
    for workdir in workdirs:
        session_dirs.extend(_sorted_children(workdir, "session_"))
    found = len(session_dirs)
    session_dirs = session_dirs[:MAX_SESSIONS]

    state_keys = KeySet()
    state_read = state_missing = state_invalid = state_oversized = 0
    wire_stats = JsonlStats()
    wire_missing = 0
    record_types: dict[str, dict[str, Any]] = {}

    for session in session_dirs:
        state_path = session / "state.json"
        try:
            with _open_regular(state_path) as handle:
                raw = handle.read(MAX_LINE_BYTES + 1)
        except OSError:
            state_missing += 1
            raw = None
        if raw is not None:
            try:
                if len(raw) > MAX_LINE_BYTES:
                    state_oversized += 1
                    raise ValueError("oversized")
                state = json.loads(raw)
            except (ValueError, RecursionError):
                state = None
                state_invalid += 1
            if isinstance(state, dict):
                state_read += 1
                state_keys.observe(state)
                titles.observe("state.json", "state", state)
            elif state is not None:
                state_invalid += 1

        wire_path = session / "agents" / "main" / "wire.jsonl"
        before_unreadable = wire_stats.files_unreadable
        for record in _scan_jsonl(wire_path, wire_stats):
            raw_type = record.get("type")
            rtype = (
                (_safe_name(raw_type) or "<unprintable>")
                if isinstance(raw_type, str)
                else "<missing>"
            )
            entry = record_types.setdefault(rtype, {"count": 0, "keys": KeySet()})
            entry["count"] += 1
            entry["keys"].observe(record)
            titles.observe("wire.jsonl", rtype, record)
        if wire_stats.files_unreadable > before_unreadable:
            wire_missing += 1

    wire_types_out: dict[str, Any] = {}
    for rtype, entry in sorted(record_types.items()):
        rendered = entry["keys"].render("records")
        out: dict[str, Any] = {"count": entry["count"], **rendered}
        if rtype == _NUMERIC_RECORD_TYPE:
            out["numeric_keys"] = sorted(
                k for k, s in entry["keys"].keys.items() if "number" in s.types
            )
        wire_types_out[rtype] = out

    index_stats = JsonlStats()
    index_keys = KeySet()
    index_records = 0
    for record in _scan_jsonl(root / "session_index.jsonl", index_stats):
        index_records += 1
        index_keys.observe(record)
        titles.observe("session_index.jsonl", "index", record)

    return {
        "limits": {
            "max_sessions": MAX_SESSIONS,
            "max_lines_per_wire_file": MAX_LINES_PER_FILE,
            "max_line_bytes": MAX_LINE_BYTES,
        },
        "sessions": {
            "found": found,
            "scanned": len(session_dirs),
            "truncated": found > len(session_dirs),
        },
        "state_json": {
            "files_read": state_read,
            "files_missing_or_unreadable": state_missing,
            "files_invalid": state_invalid,
            "files_oversized": state_oversized,
            **state_keys.render("sessions"),
        },
        "wire_jsonl": {
            **_render_jsonl_counters(wire_stats),
            "record_types": wire_types_out,
        },
        "session_index_jsonl": {
            **_render_jsonl_counters(index_stats),
            "records": index_records,
            **index_keys.render("records"),
        },
        "title_or_name_keys": titles.render(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(json.dumps({"error": "usage: kimi_structure.py <kimi-data-dir>"}))
        return 2
    root = Path(args[0])
    if not root.is_dir():
        print(json.dumps({"error": "argument is not a readable directory"}))
        return 2
    print(json.dumps(describe(root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
