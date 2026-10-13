"""Keep detect-secrets baselines portable while preserving its failure/update semantics."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from detect_secrets.pre_commit_hook import raise_exception_if_baseline_file_is_unstaged


def baseline_paths(data: dict[str, Any], separator: str) -> dict[str, Any]:
    result = copy.deepcopy(data)
    entries = {}
    for name, values in result["results"].items():
        normalized = name.replace("\\", "/").replace("/", separator)
        for item in values:
            item["filename"] = item["filename"].replace("\\", "/").replace("/", separator)
        entries[normalized] = values
    result["results"] = entries
    return result


def run_hook(baseline: Path, filenames: list[str]) -> int:
    # The temporary native copy must not bypass the original staged-baseline check.
    try:
        raise_exception_if_baseline_file_is_unstaged(str(baseline))
    except ValueError:
        return 1
    original = json.loads(baseline.read_text(encoding="utf-8"))
    # detect-secrets normally excludes its own baseline; the native copy changes
    # that filename, so retain the exclusion for the canonical baseline too.
    filenames = [name for name in filenames if Path(name).resolve() != baseline.resolve()]
    with tempfile.TemporaryDirectory(prefix="runway-secrets-") as tmp:
        native = Path(tmp) / "baseline.json"
        native.write_text(json.dumps(baseline_paths(original, os.sep), indent=2), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "detect_secrets.pre_commit_hook",
                "--baseline",
                str(native),
                *[os.path.normpath(name) for name in filenames],
            ],
            check=False,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        if result.returncode == 3:
            updated = baseline_paths(json.loads(native.read_text(encoding="utf-8")), "/")
            for rule in updated.get("filters_used", []):
                if rule.get("path") == "detect_secrets.filters.common.is_baseline_file":
                    rule["filename"] = baseline.as_posix()
            baseline.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
            print(f"Updated {baseline}; stage it before committing.")
        return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("filenames", nargs="*")
    args = parser.parse_args()
    return run_hook(args.baseline, args.filenames)


if __name__ == "__main__":
    raise SystemExit(main())
