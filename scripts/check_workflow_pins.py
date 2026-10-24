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

The container images a job runs in are pinned the same way, by digest. A tag
moves under the build exactly like a branch does, and the Playwright image
carries the browsers the browser matrix tests with, so the tag is checked
against the Playwright version the repository locks: a bump of the lockfile
that does not move the image would otherwise run the checks against browsers
the repository does not pin.

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
LOCKFILE = REPO_ROOT / "webapp" / "package-lock.json"

REVISION = re.compile(r"[0-9a-f]{40}")
USE = re.compile(r"\s*(?:-\s*)?uses:\s*([^\s#]+)")
IMAGE = re.compile(r"\s*(?:-\s*)?image:\s*([^\s#]+)")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
#: The version a container tag carries, e.g. ``v1.64.1-noble`` -> ``1.64.1``.
VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")
#: The image that carries the browsers the browser matrix runs with.
PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright"


@dataclass(frozen=True)
class Reference:
    """One external action reference in a workflow file."""

    path: str
    line: int
    action: str
    revision: str


@dataclass(frozen=True)
class Image:
    """One container image reference in a workflow file."""

    path: str
    line: int
    reference: str

    @property
    def name(self) -> str:
        """The image reference without its digest."""
        return self.reference.split("@", 1)[0]

    @property
    def repository(self) -> str:
        """The image without its tag."""
        head, separator, tail = self.name.rpartition("/")
        if separator and ":" in tail:
            # A registry can carry a port (``localhost:5000/image``).
            return f"{head}/{tail.split(':', 1)[0]}"
        return self.name.split(":", 1)[0]

    @property
    def tag(self) -> str:
        """The tag, or an empty string when the reference carries none."""
        head, separator, tail = self.name.rpartition("/")
        if separator:
            return tail.partition(":")[2]
        return self.name.partition(":")[2]

    @property
    def digest(self) -> str:
        """The digest, or an empty string when the reference carries none."""
        _, separator, digest = self.reference.partition("@")
        return digest if separator else ""

    @property
    def version(self) -> str | None:
        """The version the tag carries, e.g. ``1.64.1``."""
        match = VERSION.search(self.tag)
        return match.group(1) if match else None


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


def images(root: Path) -> list[Image]:
    """Every container image reference in the workflows under ``root``.

    A job's ``container`` and a ``services`` entry are both ``image:``
    references, and both are images the runner pulls by name.
    """
    found: list[Image] = []
    for path in sorted(root.glob("*.y*ml")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = IMAGE.match(line)
            if not match:
                continue
            found.append(Image(path.name, number, match.group(1)))
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


def locked_playwright(path: Path = LOCKFILE) -> str | None:
    """The Playwright version the repository locks, or None when it cannot tell.

    The lockfile is the record of the browser version the matrix runs against;
    the image carries the browsers themselves.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    packages = data.get("packages")
    if not isinstance(packages, dict):
        return None
    entry = packages.get("node_modules/playwright-core")
    if not isinstance(entry, dict):
        return None
    version = entry.get("version")
    return str(version) if version else None


def image_findings(root: Path, lockfile: Path = LOCKFILE) -> list[str]:
    """Everything wrong with the container images the workflows run."""
    problems: list[str] = []
    locked = locked_playwright(lockfile)
    for image in images(root):
        if not DIGEST.fullmatch(image.digest):
            problems.append(
                f"{image.path}:{image.line}: {image.reference} is not pinned to a "
                "digest. A tag moves under the build exactly like a branch does, "
                "so pin the image to its sha256 digest."
            )
            continue
        if image.repository != PLAYWRIGHT_IMAGE:
            continue
        version = image.version
        if locked is None:
            problems.append(
                f"{image.path}:{image.line}: the Playwright image {image.reference} "
                "cannot be checked against the locked Playwright, because the "
                "lockfile pins no version. A check that cannot run is not a "
                "check; the lockfile has to state the version the image "
                "carries."
            )
        elif version is None:
            problems.append(
                f"{image.path}:{image.line}: the Playwright image {image.reference} "
                "carries no version, so it cannot be checked against the locked "
                "Playwright."
            )
        elif version != locked:
            problems.append(
                f"{image.path}:{image.line}: the Playwright image is {image.tag}, "
                f"but the lockfile pins playwright-core at {locked}. The baked "
                "browsers would not match the locked Playwright; move the tag "
                "and the digest in the same change as the lockfile."
            )
    return problems


def findings(root: Path, pins: dict[str, str], lockfile: Path = LOCKFILE) -> list[str]:
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
                f"{reference.path}:{reference.line}: {reference.action} is not "
                f"recorded in {PINS_LABEL}. Verify the revision before recording "
                "it: `gh api repos/<owner>/<repo>/commits/<sha>` answers 422 for a "
                "well-formed SHA that does not exist."
            )
            continue
        if recorded != reference.revision:
            problems.append(
                f"{reference.path}:{reference.line}: {reference.action} is pinned to "
                f"{reference.revision}, but {PINS_LABEL} records {recorded}. "
                f"Update the record to {reference.revision} once the new revision "
                "is reviewed."
            )
    for action in sorted(set(pins) - used):
        problems.append(f"{action} is recorded in {PINS_LABEL}, but no workflow uses it")
    problems.extend(image_findings(root, lockfile))
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
