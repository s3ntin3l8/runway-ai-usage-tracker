"""Fail the macOS sidecar build if the Keychain helper's CDHash drifted.

macOS keys a Keychain "Always Allow" grant to the helper's code identity, so a
CDHash that differs from the golden file silently costs every user their grant.
Usage: ``python scripts/check_helper_cdhash.py [HELPER] [--golden FILE]``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "sidecar_app" / "keychain_helper" / "kc_helper.cdhash"
IDENTIFIER = "com.runway.sidecar.keychain-helper"
DEFAULT_HELPER = Path("dist/Runway Sidecar.app/Contents/Frameworks/keychain_helper/kc_helper")

CHANGED = (
    "the Keychain helper changed; users will be asked to re-authorize; "
    "update the golden file only if that is intended"
)

_CDHASH = re.compile(r"^CDHash=([0-9a-f]+)\s*$", re.MULTILINE)
_IDENTIFIER = re.compile(r"^Identifier=(.+?)\s*$", re.MULTILINE)


def parse_golden(text: str) -> dict[str, str]:
    """``<arch> <cdhash>`` lines -> {arch: cdhash}; blank lines and ``#`` comments skipped."""
    golden: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"malformed golden line: {line!r}")
        golden[parts[0]] = parts[1].lower()
    if not golden:
        raise ValueError("golden file lists no architectures")
    return golden


def parse_codesign(output: str) -> tuple[str | None, str | None]:
    """(identifier, cdhash) from ``codesign -dvvv`` output (which goes to stderr)."""
    ident = _IDENTIFIER.search(output)
    cdhash = _CDHASH.search(output)
    return (ident.group(1) if ident else None, cdhash.group(1).lower() if cdhash else None)


def compare(
    golden: dict[str, str], observed: dict[str, tuple[str | None, str | None]]
) -> list[str]:
    """Problems, empty when every golden slice matches in identifier and CDHash."""
    problems: list[str] = []
    for arch, want in sorted(golden.items()):
        ident, got = observed.get(arch, (None, None))
        if got is None:
            problems.append(f"{arch}: no CDHash could be read from the helper")
            continue
        if ident != IDENTIFIER:
            problems.append(f"{arch}: identifier is {ident!r}, expected {IDENTIFIER!r}")
        if got != want:
            problems.append(f"{arch}: CDHash {got} does not match golden {want}")
    if problems:
        problems.append(CHANGED)
    return problems


def read_codesign(helper: Path, arch: str) -> str:
    result = subprocess.run(
        ["codesign", "-dvvv", "--arch", arch, str(helper)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stderr + result.stdout


def check(
    helper: Path,
    golden_path: Path = GOLDEN,
    run: Callable[[Path, str], str] | None = None,
) -> list[str]:
    run = run or read_codesign
    golden = parse_golden(golden_path.read_text())
    if not helper.is_file():
        return [f"Keychain helper not found at {helper}"]
    observed = {arch: parse_codesign(run(helper, arch)) for arch in golden}
    return compare(golden, observed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("helper", nargs="?", type=Path, default=DEFAULT_HELPER)
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    args = parser.parse_args(argv)
    problems = check(args.helper, args.golden)
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"Keychain helper CDHash matches the golden file ({args.helper})")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
