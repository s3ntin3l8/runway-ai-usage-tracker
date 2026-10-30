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
from app.services.project_label import clean_worktree_patterns

_lock = Lock()
_cached: tuple[str, ...] | None = None
#: Bumped by every invalidation, so a read that raced a write never stores the
#: value it read before the write.
_generation = 0


def parse_stored_patterns(raw: str | None) -> tuple[str, ...]:
    """The patterns stored in the config row; a corrupt value reads as no patterns."""
    if not raw:
        return ()
    try:
        value = json.loads(raw)
        if not isinstance(value, list):
            return ()
        return tuple(clean_worktree_patterns(str(v) for v in value))
    except (ValueError, TypeError):
        return ()


def get_worktree_patterns(session: Session) -> tuple[str, ...]:
    """The operator-added patterns (the built-in containers are not included)."""
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        generation = _generation
    cfg = session.exec(select(SystemConfig)).first()
    patterns = parse_stored_patterns(cfg.worktree_patterns_json if cfg else None)
    with _lock:
        if generation == _generation:
            _cached = patterns
    return patterns


def invalidate_worktree_patterns() -> None:
    """Forget the cached patterns; the next read goes to the database."""
    global _cached, _generation
    with _lock:
        _cached = None
        _generation += 1
