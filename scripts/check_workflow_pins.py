"""Reject mutable external CI references and keep the pinned ones on record.

Every external action a workflow uses is pinned to a full commit SHA, so a tag
or a branch cannot move under the build. The shape of that pin is not enough,
though: a well-formed SHA that does not exist upstream fails the job at action
resolution, before a single step in it runs, so the check the job was there to
perform never happens. A one-character corruption of the ``actions/setup-python``
pin kept the image-policy tripwire from running for a week, and the shape check
passed it the whole time.

So the workflows are compared against ``scripts/action_pins.json``, the recorded
revision of each action, and a reference the record does not cover is reported.
The comparison runs in both directions. A reference nothing records is a pin no
one vouches for. A recorded revision no workflow uses is a record that has
drifted away from the workflows it was written for.

Usage: python scripts/check_workflow_pins.py
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PINS_FILE = REPO_ROOT / "scripts" / "action_pins.json"
PINS_LABEL = "scripts/action_pins.json"

REVISION = re.compile(r"[0-9a-f]{40}")
USE = re.compile(r"\s*(?:-\s*)?uses:\s*([^\s#]+)")


@dataclass(frozen=True)
class Reference:
    """One external action reference in a workflow file."""

    path: str
    line: int
    action: str
    revision: str


def references(root: Path) -> list[Reference]:
    """Every external action reference in the workflows under ``root``.

    A local workflow path refers to this repository, so it moves with the commit
    that uses it and carries no revision of its own.
    """
    found: list[Reference] = []
    for path in sorted(root.glob("*.y*ml")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = USE.match(line)
            if not match:
                continue
            target = match.group(1)
            if target.startswith("./"):
                continue
            action, _, revision = target.rpartition("@") if "@" in target else (target, "", "")
            found.append(Reference(path.name, number, action, revision))
    return found


def load_pins(path: Path = PINS_FILE) -> dict[str, str]:
    """The recorded revision of each external action."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"{PINS_LABEL} is missing") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{PINS_LABEL} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{PINS_LABEL} must map an action to a commit SHA")
    return {str(action): str(revision) for action, revision in data.items()}


def findings(root: Path, pins: dict[str, str]) -> list[str]:
    """Everything that has to be fixed before the workflows can be trusted."""
    problems: list[str] = []
    used: set[str] = set()
    for reference in references(root):
        recorded = pins.get(reference.action)
        if recorded is not None:
            used.add(reference.action)
        if not REVISION.fullmatch(reference.revision):
            problems.append(
                f"{reference.path}:{reference.line}: {reference.action}@{reference.revision} "
                "is not pinned to a commit SHA"
            )
            continue
        if recorded is None:
            problems.append(
                f"{reference.path}:{reference.line}: {reference.action} "
                f"is not recorded in {PINS_LABEL}"
            )
            continue
        if recorded != reference.revision:
            problems.append(
                f"{reference.path}:{reference.line}: {reference.action} is pinned to "
                f"{reference.revision}, but the recorded revision is {recorded}"
            )
    for action in sorted(set(pins) - used):
        problems.append(f"{action} is recorded in {PINS_LABEL}, but no workflow uses it")
    return problems


def main() -> int:
    try:
        pins = load_pins()
    except ValueError as exc:
        print(exc)
        return 1
    problems = findings(WORKFLOWS, pins)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
