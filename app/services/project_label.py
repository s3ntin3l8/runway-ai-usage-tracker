"""Project-label derivation from a session's working directory.

The authoritative `project` label on a usage event is the basename of the
session's root directory — the directory the agent was launched from. Two things
make that non-trivial, so they live here as the single source of truth shared by
`EventIngestor` and the consolidation migration:

- **Rule 1 — worktree-container truncation** (stateless, path-only): a
  per-message `cwd` often points inside a git worktree
  (`<root>/.claude/worktrees/<name>/…`, `<root>/.wt/<slug>/…`) or a temp dir
  (`<root>/.claude/tmp/…`). A `.claude`, `.wt`, `.worktrees`, `.worktree` or
  `.mullion-worktrees` segment reliably marks the project-root boundary, so we
  cut there. Safe to apply to a single cwd at ingest.
  The segment has to be a whole one — and it has to be *preceded* by a
  separator, so a relative path that starts with `.claude` is left alone: there
  is no root to cut back to, and cutting it would lose the label entirely.
  The operator can add more worktree directory names (`worktree_patterns` in
  Settings); `normalize_cwd` is the only place that applies them. See
  `docs/collection_logic.md` for the cut rule.
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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

#: Both separators, always: the client's OS decides which one it writes, not the
#: server's.
_SEPARATORS_RE = re.compile(r"[\\/]+")

#: Directory names that hold a repository's worktrees and temp checkouts:
#: `<root>/.claude/worktrees/<name>`, `<root>/.wt/<slug>`, ... The segment marks
#: the project-root boundary, so `normalize_cwd` cuts the path there. A bare
#: `worktrees` folder (no dot) is deliberately NOT listed: it only occurs below
#: `.claude` or agy's `.system_generated`. Keep this the one list to extend.
WORKTREE_CONTAINERS: tuple[str, ...] = (
    ".claude",
    ".wt",
    ".worktrees",
    ".worktree",
    ".mullion-worktrees",
)

_CONTAINER_NAMES = frozenset(name.lower() for name in WORKTREE_CONTAINERS)

#: A path segment together with the separator run that precedes it. A segment
#: always has a separator before it, so a relative path that starts with a
#: container name is not a project root boundary — it has no root to cut back
#: to, and cutting it would lose the label entirely.
_SEGMENT_RE = re.compile(r"[\\/]+([^\\/]+)")

#: Bounds for the operator-supplied list (`worktree_patterns`).
MAX_WORKTREE_PATTERNS = 20
MAX_WORKTREE_PATTERN_LENGTH = 64


@dataclass(frozen=True)
class _CompiledPattern:
    """One operator-supplied worktree pattern, ready to match a segment."""

    regex: re.Pattern[str]
    #: Set for a repo-sibling pattern (`*-wt`): how many trailing characters of
    #: the matched segment are the pattern's literal suffix, to be stripped to
    #: get the repository folder's name. None for a container pattern.
    sibling_suffix_len: int | None


def validate_worktree_pattern(raw: str) -> str:
    """Return the cleaned pattern, or raise ValueError saying what is wrong.

    A pattern is one path segment name: non-empty, no path separator, no control
    character, bounded length, and `*` as the only glob character. It needs at
    least one literal character, since a bare `*` would match every directory.
    """
    pattern = raw.strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    if len(pattern) > MAX_WORKTREE_PATTERN_LENGTH:
        raise ValueError(f"pattern is longer than {MAX_WORKTREE_PATTERN_LENGTH} characters")
    if "/" in pattern or "\\" in pattern:
        raise ValueError("pattern must be a single directory name without path separators")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in pattern):
        raise ValueError("pattern must not contain control characters")
    if any(ch in pattern for ch in "?[]"):
        raise ValueError("only '*' is allowed as a wildcard")
    if not pattern.replace("*", "") or pattern in (".", ".."):
        raise ValueError("pattern must contain a literal directory name")
    return pattern


def clean_worktree_patterns(raw: Iterable[str]) -> list[str]:
    """Validate a whole list: each entry, the count, and case-insensitive duplicates."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        pattern = validate_worktree_pattern(entry)
        if pattern.lower() in seen:
            continue
        seen.add(pattern.lower())
        cleaned.append(pattern)
    if len(cleaned) > MAX_WORKTREE_PATTERNS:
        raise ValueError(f"at most {MAX_WORKTREE_PATTERNS} patterns are allowed")
    return cleaned


@lru_cache(maxsize=32)
def _compile_patterns(patterns: tuple[str, ...]) -> tuple[_CompiledPattern, ...]:
    compiled: list[_CompiledPattern] = []
    for pattern in patterns:
        try:
            pattern = validate_worktree_pattern(pattern)
        except ValueError:
            continue  # a bad stored value never breaks ingest
        regex = re.compile(
            ".*".join(re.escape(part) for part in pattern.split("*")), re.IGNORECASE | re.DOTALL
        )
        suffix = pattern[1:]
        is_sibling = pattern.startswith("*") and "*" not in suffix
        compiled.append(_CompiledPattern(regex, len(suffix) if is_sibling else None))
    return tuple(compiled)


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


def normalize_cwd(cwd: str | None, extra_patterns: Sequence[str] = ()) -> str | None:
    """Rule 1: strip the volatile suffix from a cwd. Path-only, stateless.

    Walks the path's segments left to right and acts on the FIRST one that is a
    worktree directory: a built-in container (`WORKTREE_CONTAINERS`: `.claude`,
    `.wt`, ...) or one matching an operator pattern (`extra_patterns`, the
    `worktree_patterns` setting; `*` is the only wildcard).

    - A *container* (every built-in, and any pattern that does not start with
      `*`) is cut off together with everything after it: `<root>/.wt/<slug>/app`
      becomes `<root>`.
    - A pattern that starts with `*` followed by a literal suffix (`*-wt`) names
      a folder kept *beside* the repository, so the segment is the repository's
      sibling: it is cut after the segment and the suffix is stripped from it,
      `/home/u/repo-wt/<slug>/app` becomes `/home/u/repo`. A segment that is
      nothing but the suffix has no repository name left and is cut like a
      container.

    The segment is matched whole, in either separator form and any case, and
    must be preceded by a separator — a relative path that *starts* with such a
    segment has no root to cut back to and is left alone. Trailing separators
    are stripped. Returns None for None/empty input.
    """
    if not cwd:
        return None
    patterns = _compile_patterns(tuple(extra_patterns)) if extra_patterns else ()
    for match in _SEGMENT_RE.finditer(cwd):
        segment = match.group(1)
        if segment.lower() in _CONTAINER_NAMES:
            cwd = cwd[: match.start()]
            break
        hit = next((p for p in patterns if p.regex.fullmatch(segment)), None)
        if hit is None:
            continue
        if hit.sibling_suffix_len is not None and len(segment) > hit.sibling_suffix_len:
            cwd = cwd[: match.end() - hit.sibling_suffix_len]
        else:
            cwd = cwd[: match.start()]
        break
    cwd = cwd.rstrip("/\\")
    return cwd or None


def derive_project(cwd: str | None, extra_patterns: Sequence[str] = ()) -> str | None:
    """The project label: the last segment of the rule-1-normalized cwd.

    The last *path segment*, not the last separator-delimited chunk of the
    server's flavour: `D:\\Projects\\repo` and `/home/u/repo` both label as
    `repo`. A path that holds nothing but a drive spec (`C:\\`) has no label.
    """
    normalized = normalize_cwd(cwd, extra_patterns)
    if not normalized:
        return None
    segments = _segments(normalized)
    if segments and _is_drive_spec(segments[0]):
        segments = segments[1:]
    if not segments:
        return None
    return segments[-1]


def pick_canonical_cwd(
    cwds: Iterable[str | None], extra_patterns: Sequence[str] = ()
) -> str | None:
    """Rule 2: the canonical (root) cwd for a session.

    Normalizes each cwd (rule 1) then picks the shallowest — fewest path
    segments, tie-broken by shortest string and then lexicographically, so the
    choice is the same on every host. Returns None when no usable cwd exists.
    """
    normalized = [c for c in (normalize_cwd(c, extra_patterns) for c in cwds) if c]
    if not normalized:
        return None
    return min(normalized, key=lambda c: (_depth(c), len(c), c))
