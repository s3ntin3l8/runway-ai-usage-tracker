"""Fail when the shipped image-policy exception document needs a renewal.

The unit suite carries the same check, so a developer sees it locally. This
script is the scheduled counterpart: the exception document has one expiry
horizon, and nothing else in the pipeline fails when that horizon arrives --
the image builds keep passing with the entries they already match, so the
lapse would otherwise stay invisible until the horizon itself.

Usage: python -m scripts.check_image_exceptions
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.image_policy import load_exceptions, renewal_due

DEFAULT_EXCEPTIONS = Path(__file__).resolve().parents[1] / "docs/security/image-exceptions.json"


def main() -> int:
    now = datetime.now(UTC)
    try:
        loaded = load_exceptions(DEFAULT_EXCEPTIONS, now)
    except ValueError as exc:
        print(f"Image exception document is invalid: {exc}")
        return 1
    due = renewal_due(loaded, now)
    if not due:
        return 0
    print(
        "The image exception document needs a renewal "
        f"({len(due)} of {len(loaded.active) + len(loaded.expired)} entries):"
    )
    for entry in due:
        print(
            f"  {entry['id']} {entry['package']} {entry['version']} "
            f"{entry['architecture']} expires {entry['expires_at']}"
        )
    print("Renew them in a pull request; see docs/security/image-policy.md.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
