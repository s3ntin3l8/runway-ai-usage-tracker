"""Reject mutable external CI references; local workflow paths follow the commit."""

import re
import sys
from pathlib import Path


def mutable_references(root: Path) -> list[str]:
    findings = []
    for path in sorted(root.glob("*.y*ml")):
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            match = re.match(r"\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
            if not match:
                continue
            target = match[1]
            if target.startswith("./"):
                continue
            if "@" not in target or not re.fullmatch(r"[0-9a-f]{40}", target.rsplit("@", 1)[1]):
                findings.append(f"{path.name}:{line_number}: {target}")
    return findings


if __name__ == "__main__":
    issues = mutable_references(Path(__file__).resolve().parents[1] / ".github/workflows")
    for issue in issues:
        print(issue)
    sys.exit(bool(issues))
