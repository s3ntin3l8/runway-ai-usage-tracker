"""The committed agent skill and reviewer exist in two places; they must not drift.

``.claude/`` is what Claude Code reads, ``.agents/skills/`` is what codex reads
(codex does not read ``.claude/agents``). Both are real files rather than
symlinks so a Windows checkout with ``core.symlinks=false`` keeps them. The
Mullion scaffold writes both; a hand edit to only one would silently split them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_CLAUDE = ROOT / ".claude" / "skills" / "runway" / "SKILL.md"
SKILL_AGENTS = ROOT / ".agents" / "skills" / "runway" / "SKILL.md"
REVIEWER_CLAUDE = ROOT / ".claude" / "agents" / "runway-reviewer.md"
REVIEWER_AGENTS = ROOT / ".agents" / "skills" / "runway-reviewer" / "SKILL.md"


def _body(path: Path) -> str:
    """The text after the YAML frontmatter (the two copies differ only there)."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    _, sep, body = text.partition("\n---\n")
    assert sep, f"{path} has an unterminated frontmatter block"
    return body.strip()


def test_the_skill_copies_are_identical():
    assert SKILL_AGENTS.read_bytes() == SKILL_CLAUDE.read_bytes(), (
        ".agents/skills/runway/SKILL.md differs from .claude/skills/runway/SKILL.md; "
        "edit one and copy it to the other (or re-run the Mullion scaffold)."
    )


def test_the_reviewer_copies_have_the_same_body():
    assert _body(REVIEWER_AGENTS) == _body(REVIEWER_CLAUDE), (
        ".agents/skills/runway-reviewer/SKILL.md and .claude/agents/runway-reviewer.md "
        "differ beyond their frontmatter."
    )


@pytest.mark.parametrize("path", [SKILL_CLAUDE, REVIEWER_CLAUDE])
def test_the_claude_copies_are_not_gitignored(path: Path):
    """``.claude/`` is mostly local state, but the skill and reviewer are committed."""
    if shutil.which("git") is None:
        pytest.skip("git is required to test the ignore rules")
    # --no-index: ask what the rules say, whether or not the file is tracked.
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path.relative_to(ROOT).as_posix()],
        cwd=ROOT,
        capture_output=True,
    )
    assert result.returncode == 1, f"{path.relative_to(ROOT)} is ignored by .gitignore"
