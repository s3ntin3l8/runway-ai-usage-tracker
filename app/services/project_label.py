"""Project-label derivation from a session's working directory.

The authoritative `project` label on a usage event is the basename of the
session's root directory — the directory the agent was launched from. Two things
make that non-trivial, so they live here as the single source of truth shared by
`EventIngestor` and the consolidation migration:

- **Rule 1 — `.claude/` truncation** (stateless, path-only): a per-message `cwd`
  often points inside a git worktree (`<root>/.claude/worktrees/<name>/…`) or a
  temp dir (`<root>/.claude/tmp/…`). The `/.claude/` segment reliably marks the
  project-root boundary, so we cut there. Safe to apply to a single cwd at ingest.
- **Rule 2 — per-session shallowest cwd**: Claude Code records a `cwd` that drifts
  into subfolders (`services/api`, `apps/web`, …) mid-session. The shallowest cwd
  observed in a session is the root. This needs the whole session, so it runs in
  the offline migration, never the hot path. Deliberately NOT a global
  longest-prefix collapse — that would swallow projects launched under shared
  ancestors like `/home` or `/home/user/projects`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

_CLAUDE_SEGMENT = "/.claude/"


def normalize_cwd(cwd: str | None) -> str | None:
    """Rule 1: strip the volatile suffix from a cwd. Path-only, stateless.

    If `cwd` contains `/.claude/`, truncate to the part before the FIRST
    occurrence (drops worktrees/tmp). Trailing slashes are stripped. Returns
    None for None/empty input.
    """
    if not cwd:
        return None
    idx = cwd.find(_CLAUDE_SEGMENT)
    if idx != -1:
        cwd = cwd[:idx]
    cwd = cwd.rstrip("/")
    return cwd or None


def derive_project(cwd: str | None) -> str | None:
    """The project label: basename of the rule-1-normalized cwd, or None."""
    normalized = normalize_cwd(cwd)
    return os.path.basename(normalized) if normalized else None


def pick_canonical_cwd(cwds: Iterable[str | None]) -> str | None:
    """Rule 2: the canonical (root) cwd for a session.

    Normalizes each cwd (rule 1) then picks the shallowest — fewest path
    separators, tie-broken by shortest string. Returns None when no usable cwd
    exists.
    """
    normalized = [c for c in (normalize_cwd(c) for c in cwds) if c]
    if not normalized:
        return None
    return min(normalized, key=lambda c: (c.count("/"), len(c)))
