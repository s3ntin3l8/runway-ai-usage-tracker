"""Project-label derivation from a session's working directory.

The authoritative `project` label on a usage event is the basename of the
session's root directory — the directory the agent was launched from. Two things
make that non-trivial, so they live here as the single source of truth shared by
`EventIngestor` and the consolidation migration:

- **Rule 1 — `.claude/` truncation** (stateless, path-only): a per-message `cwd`
  often points inside a git worktree (`<root>/.claude/worktrees/<name>/…`) or a
  temp dir (`<root>/.claude/tmp/…`). The `.claude` segment reliably marks the
  project-root boundary, so we cut there. Safe to apply to a single cwd at ingest.
- **Rule 2 — per-session shallowest cwd**: Claude Code records a `cwd` that drifts
  into subfolders (`services/api`, `apps/web`, …) mid-session. The shallowest cwd
  observed in a session is the root. This needs the whole session, so it runs in
  the offline migration, never the hot path. Deliberately NOT a global
  longest-prefix collapse — that would swallow projects launched under shared
  ancestors like `/home` or `/home/user/projects`.

**The derivation is host-independent.** A client's `cwd` is a path *on that
client's machine*: a Windows sidecar reports `D:\\Projects\\repo`, a macOS or
Linux one reports `/home/u/repo`. The server runs on Linux, so `os.path` is
`posixpath` and knows only `/` as a separator — a backslash path then has
nothing to split on and the whole path would become the label. The rules below
therefore treat `/` and `\\` as the same separator and never consult the
*server's* idea of a path, so the same repository gets the same label from every
host. This is why `os.path` is not used here at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Both separators, always: the client's OS decides which one it writes, not the
#: server's.
_SEPARATORS_RE = re.compile(r"[\\/]+")

#: A path segment named `.claude` marks the project-root boundary. The segment
#: must be a whole segment (`\.claude` followed by a separator or the end of the
#: path), and the match is case-insensitive because Windows paths are.
_CLAUDE_SEGMENT_RE = re.compile(r"(?:^|[\\/])\.claude(?=[\\/]|$)", re.IGNORECASE)

#: A Windows drive spec (`C:`). It is not a directory level, so it does not count
#: towards a path's depth and never becomes the label.
_DRIVE_SPEC_RE = re.compile(r"^[A-Za-z]:$")


def _segments(path: str) -> list[str]:
    """The path's segments, split on either separator; empty ones dropped."""
    return [segment for segment in _SEPARATORS_RE.split(path) if segment]


def _is_drive_spec(segment: str) -> bool:
    """True for a Windows drive spec such as `C:`."""
    return bool(_DRIVE_SPEC_RE.match(segment))


def _depth(path: str) -> int:
    """How many directory levels the path has. The drive spec does not count."""
    segments = _segments(path)
    if segments and _is_drive_spec(segments[0]):
        segments = segments[1:]
    return len(segments)


def normalize_cwd(cwd: str | None) -> str | None:
    """Rule 1: strip the volatile suffix from a cwd. Path-only, stateless.

    If `cwd` contains a `.claude` segment, truncate to the part before the FIRST
    occurrence (drops worktrees/tmp). The segment is matched in either
    separator form, so a Windows worktree path collapses like a POSIX one.
    Trailing separators are stripped. Returns None for None/empty input.
    """
    if not cwd:
        return None
    match = _CLAUDE_SEGMENT_RE.search(cwd)
    if match is not None:
        cwd = cwd[: match.start()]
    cwd = cwd.rstrip("/\\")
    return cwd or None


def derive_project(cwd: str | None) -> str | None:
    """The project label: the last segment of the rule-1-normalized cwd.

    The last *path segment*, not the last separator-delimited chunk of the
    server's flavour: `D:\\Projects\\repo` and `/home/u/repo` both label as
    `repo`. A path that holds nothing but a drive spec (`C:\\`) has no label.
    """
    normalized = normalize_cwd(cwd)
    if not normalized:
        return None
    segments = _segments(normalized)
    if segments and _is_drive_spec(segments[0]):
        segments = segments[1:]
    if not segments:
        return None
    return segments[-1]


def pick_canonical_cwd(cwds: Iterable[str | None]) -> str | None:
    """Rule 2: the canonical (root) cwd for a session.

    Normalizes each cwd (rule 1) then picks the shallowest — fewest path
    segments, tie-broken by shortest string and then lexicographically, so the
    choice is the same on every host. Returns None when no usable cwd exists.
    """
    normalized = [c for c in (normalize_cwd(c) for c in cwds) if c]
    if not normalized:
        return None
    return min(normalized, key=lambda c: (_depth(c), len(c), c))
