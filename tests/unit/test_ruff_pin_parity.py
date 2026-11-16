"""Each tool pinned in both ``.pre-commit-config.yaml`` and a requirements file must match.

Dependabot bumps the pre-commit ``rev:`` and the pip pin in two independent
pull requests, so one can merge a cycle before the other. For ruff that means a
local ``ruff --fix`` rewrites code differently from the CI lint gate; for
detect-secrets it means the hook and ``scripts/check_secrets.py`` (which imports
and string-matches its internals) run different versions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# (pre-commit repo, pip package, requirements file that pins it)
PAIRS = [
    ("https://github.com/astral-sh/ruff-pre-commit", "ruff", "requirements-lint.txt"),
    ("https://github.com/Yelp/detect-secrets", "detect-secrets", "requirements-shared.txt"),
]


def _pip_pin(text: str, package: str) -> str:
    # A trailing `# comment` is allowed after the pin.
    match = re.search(rf"^{re.escape(package)}==(\S+)[ \t]*(?:#.*)?$", text, re.MULTILINE)
    assert match, f"no `{package}==<version>` line (optionally followed by a # comment) found"
    return match.group(1)


def _hook_rev(config: dict[str, Any], repo: str) -> str:
    revs = [r["rev"] for r in config["repos"] if r.get("repo") == repo]
    assert len(revs) == 1, f"expected one {repo} entry, found {len(revs)}"
    return str(revs[0]).removeprefix("v")


@pytest.mark.parametrize(("repo", "package", "requirements"), PAIRS, ids=[p[1] for p in PAIRS])
def test_pre_commit_rev_matches_the_pip_pin(repo: str, package: str, requirements: str) -> None:
    pip = _pip_pin((ROOT / requirements).read_text(encoding="utf-8"), package)
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = _hook_rev(config, repo)
    assert hook == pip, (
        f"{repo} rev is {hook} but {requirements} pins {package}=={pip}; "
        "bump both together so local hooks and the CI gate agree"
    )


def test_parity_helpers_detect_a_mismatch_and_read_a_commented_pin() -> None:
    repo = PAIRS[0][0]
    config = {"repos": [{"repo": repo, "rev": "v0.17.4"}]}
    assert _hook_rev(config, repo) != _pip_pin("ruff==0.17.5\n", "ruff")
    assert _hook_rev(config, repo) == _pip_pin("ruff==0.17.4  # keep with the hook\n", "ruff")
