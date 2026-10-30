"""The operator's extra worktree directory patterns, read through a cached getter.

`normalize_cwd` (`app/services/project_label.py`) applies the patterns; this
module only fetches them from `system_config.worktree_patterns_json`. Ingest
labels every event, so the lookup must not cost a query per event: the value is
held in process memory and dropped when Settings writes it (single-worker
deployment, like `app/core/cache.py`).
"""

from __future__ import annotations

import json
from threading import Lock

from sqlmodel import Session, select

from app.models.db import SystemConfig
from app.services.project_label import MAX_WORKTREE_PATTERNS, validate_worktree_pattern


class _State:
    """The process-wide cache: the patterns and a generation counter."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.patterns: tuple[str, ...] | None = None
        #: Bumped by every invalidation, so a read that raced a write never
        #: stores the value it read before the write.
        self.generation = 0


_state = _State()


def parse_stored_patterns(raw: str | None) -> tuple[str, ...]:
    """The patterns stored in the config row.

    A corrupt value reads as no patterns, and a single bad entry is skipped
    rather than disabling the rest.
    """
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(value, list):
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for entry in value:
        try:
            pattern = validate_worktree_pattern(entry) if isinstance(entry, str) else None
        except ValueError:
            pattern = None
        if pattern is None or pattern.lower() in seen:
            continue
        seen.add(pattern.lower())
        cleaned.append(pattern)
    return tuple(cleaned[:MAX_WORKTREE_PATTERNS])


def get_worktree_patterns(session: Session) -> tuple[str, ...]:
    """The operator-added patterns (the built-in containers are not included)."""
    with _state.lock:
        if _state.patterns is not None:
            return _state.patterns
        generation = _state.generation
    cfg = session.exec(select(SystemConfig)).first()
    patterns = parse_stored_patterns(cfg.worktree_patterns_json if cfg else None)
    with _state.lock:
        if generation == _state.generation:
            _state.patterns = patterns
    return patterns


def invalidate_worktree_patterns() -> None:
    """Forget the cached patterns; the next read goes to the database."""
    with _state.lock:
        _state.patterns = None
        _state.generation += 1
