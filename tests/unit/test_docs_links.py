"""Keep the Markdown documentation internally consistent.

Two failure classes this catches, both of which have shipped before:

* a relative link that points at a file that no longer exists (or never
  existed) - e.g. ``[.env.example](file:///.env.example)``;
* a link into a Markdown file with an ``#anchor`` that matches no heading,
  usually because the heading was renamed without updating the link.

The check is deliberately structural. It asserts that the documentation graph
resolves, not that the prose is good.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Directories that hold no project documentation.
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".venv-sc",
    ".wt",
    ".superpowers",
    "node_modules",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
}

# ``[text](target)`` - the target may be wrapped in <> or quoted.
LINK_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(<[^>]*>|\"[^\"]*\"|'[^']*'|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)"
)
# A ``file://`` URI is never a valid way to reference a repo file on GitHub.
FILE_URI_RE = re.compile(r"\]\(\s*file://", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _markdown_files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*.md"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return sorted(found)


def _slug(text: str) -> str:
    """Approximate GitHub's heading anchor algorithm."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = "".join(
        ch
        for ch in text
        if ch.isalnum() or ch in {" ", "-", "_"} or unicodedata.category(ch).startswith("M")
    )
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    """Every anchor a Markdown file exposes, including duplicate-heading suffixes."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if not match:
            continue
        base = _slug(match.group(2))
        if not base:
            continue
        seen = counts.get(base, 0)
        counts[base] = seen + 1
        anchors.add(base if seen == 0 else f"{base}-{seen}")
    return anchors


def _target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _links_in(text: str) -> list[tuple[int, re.Match[str]]]:
    """Every markdown link in ``text``, with the line it is on.

    Fenced code blocks are skipped, the same way ``_anchors`` skips them. A
    ```markdown block that illustrates a link - ``[example](not-a-real.md)`` -
    is an example of a link, not a link, and a troubleshooting page full of
    such blocks must not fail the build.
    """
    found: list[tuple[int, re.Match[str]]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        found.extend((number, match) for match in LINK_RE.finditer(line))
    return found


def test_no_file_uri_links() -> None:
    """``file://`` links do not resolve on GitHub and were used by mistake once."""
    offenders: list[str] = []
    for path in _markdown_files():
        in_fence = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                # A fenced block may illustrate the mistake this test names.
                continue
            if FILE_URI_RE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, "file:// links do not resolve: " + ", ".join(offenders)


def test_relative_links_resolve() -> None:
    """Every relative link points at a file that exists."""
    broken: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for number, match in _links_in(text):
            target = _target(match.group(1))
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = target.split("#", 1)[0]
            if not relative:
                continue
            resolved = (path.parent / relative).resolve()
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT)}:{number} -> {target}")
    assert not broken, "unresolved relative links:\n  " + "\n  ".join(broken)


def test_anchors_resolve() -> None:
    """Every ``#anchor`` into a Markdown file matches a heading in that file."""
    broken: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for number, match in _links_in(text):
            target = _target(match.group(1))
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if "#" not in target:
                continue
            relative, _, anchor = target.partition("#")
            if not anchor:
                continue
            if not relative:
                destination = path
            else:
                destination = (path.parent / relative).resolve()
            if not destination.exists() or destination.suffix != ".md":
                # Non-Markdown targets are checked by the link-resolution test.
                continue
            if anchor.lower() not in _anchors(destination):
                broken.append(
                    f"{path.relative_to(ROOT)}:{number} -> {target}"
                    f" (no such heading in {destination.relative_to(ROOT)})"
                )
    assert not broken, "unresolved anchors:\n  " + "\n  ".join(broken)


def test_every_doc_is_indexed() -> None:
    """Every document appears in the index that covers its directory.

    Each directory in the documentation tree has an index. A file is covered
    by the **nearest** index above it, so a collector document belongs to
    ``docs/collectors/README.md`` and not to the top-level index as well —
    that is what makes it appear in exactly one index.
    """
    docs_root = ROOT / "docs"
    assert (docs_root / "README.md").exists(), "docs/README.md is missing"

    missing: list[str] = []
    for path in sorted(docs_root.rglob("*.md")):
        relative = path.relative_to(docs_root)
        if relative.parts[0] == "README.md" and len(relative.parts) == 1:
            continue
        # Walk up to the nearest directory that has an index.
        index = None
        for parent in path.parents:
            if parent == docs_root.parent:
                break
            candidate = parent / "README.md"
            if candidate.exists() and candidate != path:
                index = candidate
                break
        if index is None:
            missing.append(f"{path.relative_to(ROOT)} (no index above it)")
            continue
        text = index.read_text(encoding="utf-8")
        indexed_root = index.parent
        candidates = {
            str(path.relative_to(indexed_root)),
            path.name,
            str(relative),
        }
        # The document has to be *linked* from the index, not merely named in
        # its prose: a passing mention is not an entry. The link may carry an
        # anchor, so `](name)` and `](name#section)` both count.
        linked = any(
            re.search(r"\]\(" + re.escape(candidate) + r"(?:#[^)]*)?\)", text)
            for candidate in candidates
        )
        if not linked:
            missing.append(f"{path.relative_to(ROOT)} (not in {index.relative_to(ROOT)})")
    assert not missing, "documents missing from their index:\n  " + "\n  ".join(missing)
