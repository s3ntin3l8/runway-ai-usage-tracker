"""Shared test helpers, markers, and fixtures for Hermes event extractor tests."""

import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.sidecar_pkg.event_extractors import hermes as hermes_extractor
from scripts.sidecar_pkg.event_extractors.hermes import (
    parse_hermes_events,
)
from tests.fixtures.hermes_fixture import make_hermes_db

# A Windows path cannot hold a `|`: the character is reserved there, in a
# directory name as much as in a file name. A database whose path holds one
# is a thing that cannot exist there, so the behaviour under test has no
# subject on that platform.
requires_pipe_in_path = pytest.mark.skipif(
    os.name == "nt",
    reason="a Windows path cannot hold a `|`",
)


def _make_db(directory: Path) -> tuple[Path, sqlite3.Connection]:
    """Create a SQLite file in ``directory`` with the Hermes schema and sample data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", dir=directory, delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = make_hermes_db(str(db_path))
    conn.close()
    return db_path, conn


def _set_row_times(db_path: Path, *, last_seen: float) -> None:
    """Move every usage row's timestamps to ``last_seen``."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE session_model_usage SET last_seen = ?, first_seen = ?",
        (last_seen, last_seen),
    )
    conn.commit()
    conn.close()


def _bump_usage(db_path: Path, session_id: str, *, input_tokens: int) -> None:
    """Add tokens to a session's usage row."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE session_model_usage SET input_tokens = input_tokens + ? WHERE session_id = ?",
        (input_tokens, session_id),
    )
    conn.commit()
    conn.close()


def _cutoff(seconds: float = 30 * 86400) -> float:
    """The idle cutoff a prune uses, pinned by the test rather than by the
    implementation's constant: a test that derives its expectation from
    ``WATERMARK_PRUNE_IDLE_DAYS`` moves with it and cannot catch a guard that
    was weakened.
    """
    return datetime.now(UTC).timestamp() - seconds


def _read_watermark(state_file: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Read the watermark log into its nested state."""
    state, _ = hermes_extractor._load_hermes_watermark(state_file)
    return state


def _watermark_entries(state_file: Path) -> dict[str, dict[str, Any]]:
    """The watermark as a flat map of "{database}|{account}|{slice}" -> mark."""
    return {
        f"{db_path}|{account}|{slice_key}": mark
        for db_path, accounts in _read_watermark(state_file).items()
        for account, slices in accounts.items()
        for slice_key, mark in slices.items()
    }


def _slice_count(state: dict[str, dict[str, dict[str, Any]]]) -> int:
    """The number of slices in a nested watermark state."""
    return sum(len(slices) for accounts in state.values() for slices in accounts.values())


def _watermark_keys(state_file: Path) -> list[str]:
    if not state_file.exists():
        return []
    return list(_watermark_entries(state_file))


def _parse(db_path: Path | list[Path], state_file: Path, account_id: str = "default"):
    """Collect from ``db_path`` against a watermark file."""
    paths = db_path if isinstance(db_path, list) else [db_path]
    return parse_hermes_events(
        db_paths=paths,
        account_id=account_id,
        since=datetime(2020, 1, 1, tzinfo=UTC),
        state_file=state_file,
    )


def _delete_usage_rows(db_path: Path, session_id: str) -> None:
    """Delete a session's usage rows, the way a deleted Hermes session does."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM session_model_usage WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
